"""Real loopback SMTP/TLS protocol tests against an invented local sink only."""
import base64
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading

import pytest

from lg_sports_deck.unified.app import create_app
from lg_sports_deck.unified.domain import MANILA
from lg_sports_deck.unified.notifications import render_test_mime
from lg_sports_deck.unified.smtp_lab import LoopbackSMTP, LoopbackSMTPWorker


PW='Another fictional password 47'
DAY=date(2027,6,2)


class Clock:
    def __init__(self):self.t=datetime(2027,6,2,10,0,tzinfo=MANILA).timestamp()
    def __call__(self):return self.t


class SMTPHandler(socketserver.BaseRequestHandler):
    """Minimal test-only fake sink, never a relay; no DNS or external delivery."""
    def handle(self):
        conn=self.request;reader=conn.makefile('rb');tls=False;authorized=False;sender=None;recipient=None
        def say(text):conn.sendall((text+'\r\n').encode('ascii'))
        try:
            say('220 local-fictional-sink.invalid ESMTP')
            while True:
                raw=reader.readline(16385)
                if not raw or len(raw)>16384:break
                line=raw.decode('ascii','replace').rstrip('\r\n');verb=line.split(' ',1)[0].upper()
                if verb in ('EHLO','HELO'):
                    if not tls:
                        if self.server.mode=='no_starttls':say('250 fictional-sink')
                        else:conn.sendall(b'250-fictional-sink\r\n250 STARTTLS\r\n')
                    else:conn.sendall(b'250-fictional-sink\r\n250-AUTH PLAIN\r\n250 SIZE 262144\r\n')
                elif verb=='STARTTLS' and not tls and self.server.mode!='no_starttls':
                    say('220 Ready for fictional TLS');reader.close()
                    conn=self.server.context.wrap_socket(conn,server_side=True)
                    reader=conn.makefile('rb');tls=True
                    with self.server.lock:self.server.tls_count+=1
                elif verb=='AUTH' and tls:
                    with self.server.lock:self.server.auth_count+=1
                    try:
                        parts=line.split(' ')
                        data=base64.b64decode(parts[2],validate=True).decode('utf-8').split('\x00')
                        authorized=(len(data)==3 and data[1]==self.server.username and data[2]==self.server.password)
                    except (ValueError,IndexError,UnicodeError):authorized=False
                    if authorized:say('235 2.7.0 Fictional authentication accepted')
                    else:say('535 5.7.8 Fictional authentication rejected')
                elif verb=='MAIL' and tls and authorized:
                    sender=line.split(':',1)[-1].split('>',1)[0].lstrip('<')
                    if sender.endswith('.invalid') or sender in self.server.allowed_mailboxes:say('250 fictional sender accepted')
                    else:say('550 only fictional senders')
                elif verb=='RCPT' and tls and authorized and sender:
                    recipient=line.split(':',1)[-1].split('>',1)[0].lstrip('<')
                    if not (recipient.endswith('.invalid') or recipient in self.server.allowed_mailboxes):say('550 only fictional recipients')
                    elif self.server.mode=='rcpt_450':say('450 fictional temporary recipient failure')
                    elif self.server.mode=='rcpt_550':say('550 fictional permanent recipient failure')
                    else:say('250 fictional recipient accepted')
                elif verb=='DATA' and tls and authorized and recipient and self.server.mode not in ('rcpt_450','rcpt_550'):
                    say('354 send fictional data')
                    lines=[]
                    while True:
                        part=reader.readline(262145)
                        if not part: return
                        if part==b'.\r\n':break
                        if part.startswith(b'..'):part=part[1:]
                        lines.append(part)
                        if sum(map(len,lines))>262144:return
                    body=b''.join(lines)
                    if self.server.mode=='data_450':say('450 fictional temporary DATA rejection')
                    elif self.server.mode=='data_550':say('550 fictional permanent DATA rejection')
                    else:
                        with self.server.lock:self.server.messages.append((sender,recipient,body))
                        if self.server.on_data:self.server.on_data()
                        if self.server.mode=='accept_drop':
                            conn.shutdown(socket.SHUT_RDWR);return
                        say('250 fictional sink accepted; not delivered')
                elif verb=='RSET':sender=None;recipient=None;say('250 reset')
                elif verb=='NOOP':say('250 ok')
                elif verb=='QUIT':say('221 bye');return
                else:say('503 command not allowed in fictional sink')
        except (OSError,ssl.SSLError):
            pass
        finally:
            try:reader.close()
            except OSError:pass
            try:conn.close()
            except OSError:pass


class Sink(socketserver.ThreadingTCPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,certificate,key,username,password):
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version=ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(certificate,key)
        self.username=username;self.password=password;self.mode='accept';self.on_data=None
        self.allowed_mailboxes=set()  # explicit reserved example.com fixtures only; no relay
        self.messages=[];self.tls_count=0;self.auth_count=0;self.lock=threading.Lock()
        super().__init__(('127.0.0.1',0),SMTPHandler)
    def handle_error(self,request,client_address):
        pass  # test sink must not print fictional credentials or message contents


@pytest.fixture(scope='module')
def certificate(tmp_path_factory):
    if not shutil.which('openssl'):pytest.skip('openssl is needed to generate an ephemeral local test TLS certificate')
    d=tmp_path_factory.mktemp('fictional-smtp-tls');cert=d/'fake-cert.pem';key=d/'fake-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','1',
                    '-keyout',str(key),'-out',str(cert),'-subj','/CN=127.0.0.1',
                    '-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return cert,key


@pytest.fixture
def smtp_world(tmp_path,certificate):
    clock=Clock();app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'media',{'https://testserver'},clock=clock)
    owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Owner',PW)
    player=app.state.accounts.register_test_player('player@example.invalid','Player',PW)
    app.state.accounts.accept_test_notice(player)
    app.state.unified.book_court(player,DAY,12,1,1,[player])
    username='sender@example.invalid';password='LG_FAKE_'+secrets.token_urlsafe(32)
    cert,key=certificate;sink=Sink(str(cert),str(key),username,password)
    thread=threading.Thread(target=sink.serve_forever,daemon=True);thread.start()
    transport=LoopbackSMTP(sink.server_address[1],cert.read_text(),username,password,timeout=2)
    worker=LoopbackSMTPWorker(app.state.engine.db,clock,transport,sender=username,
                              app_origin='https://portal.example.invalid',max_attempts=2)
    yield app,owner,player,clock,sink,transport,worker
    sink.shutdown();sink.server_close();thread.join(timeout=4);app.state.recorder.close()


def test_loopback_starttls_auth_and_single_fictional_message(smtp_world):
    app,owner,player,clock,sink,transport,worker=smtp_world
    result=worker.run_one()
    assert result['mode']=='fictional-loopback-only' and result['state']=='simulated'
    assert result['test_stage']=='fake_sink_accepted' and sink.tls_count==sink.auth_count==1
    assert len(sink.messages)==1
    envelope_from,envelope_to,body=sink.messages[0]
    assert envelope_from=='sender@example.invalid' and envelope_to=='player@example.invalid'
    msg=BytesParser(policy=policy.default).parsebytes(body)
    assert msg['X-LG-Test-Only']=='yes' and 'TEST ONLY' in msg.get_content()
    assert 'No real charge, booking or email' in msg.get_content()
    assert msg['To']=='player@example.invalid' and not msg.get('Cc') and not msg.get('Bcc')
    assert worker.run_one() is None
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT state,attempts FROM unified_test_mail_jobs').fetchone()[:]==('simulated',1)
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==1


@pytest.mark.parametrize('mode', ['rcpt_450','data_450'])
def test_known_temporary_rejection_retries_after_backoff(smtp_world,mode):
    app,owner,player,clock,sink,transport,worker=smtp_world
    sink.mode=mode;first=worker.run_one()
    assert first['test_outcome']=='transient' and first['state']=='retry_wait' and not sink.messages
    assert worker.run_one() is None
    sink.mode='accept';clock.t+=60
    second=worker.run_one();assert second['test_outcome']=='success' and second['state']=='simulated'
    assert len(sink.messages)==1
    with app.state.engine.db.read() as c:assert c.execute('SELECT attempts FROM unified_test_mail_jobs').fetchone()[0]==2


@pytest.mark.parametrize('mode', ['rcpt_550','data_550'])
def test_known_permanent_rejection_is_not_retried(smtp_world,mode):
    app,owner,player,clock,sink,transport,worker=smtp_world
    sink.mode=mode;result=worker.run_one()
    assert result['test_outcome']=='permanent' and result['state']=='failed' and not sink.messages
    clock.t+=10000;sink.mode='accept';assert worker.run_one() is None
    with app.state.engine.db.read() as c:assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_connection_loss_after_data_is_uncertain_not_blindly_retried(smtp_world):
    app,owner,player,clock,sink,transport,worker=smtp_world
    sink.mode='accept_drop';result=worker.run_one()
    assert len(sink.messages)==1  # sink consumed it before intentionally dropping the response
    assert result['test_outcome']=='unknown' and result['state']=='uncertain'
    clock.t+=10000;sink.mode='accept';assert worker.run_one() is None and len(sink.messages)==1
    with app.state.engine.db.read() as c:assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_untrusted_tls_fails_before_auth_or_message(smtp_world,tmp_path):
    app,owner,player,clock,sink,transport,worker=smtp_world
    cert=tmp_path/'other-cert.pem';key=tmp_path/'other-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','1',
                    '-keyout',str(key),'-out',str(cert),'-subj','/CN=127.0.0.1',
                    '-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    bad=LoopbackSMTP(sink.server_address[1],cert.read_text(),sink.username,sink.password,timeout=2)
    bad_worker=LoopbackSMTPWorker(app.state.engine.db,clock,bad,sender=sink.username,app_origin='https://portal.example.invalid')
    result=bad_worker.run_one()
    assert result['state']=='failed' and result['test_stage']=='test_tls_or_auth_failed'
    assert sink.auth_count==0 and not sink.messages


def test_missing_starttls_and_invalid_test_auth_never_deliver(smtp_world,certificate):
    app,owner,player,clock,sink,transport,worker=smtp_world
    cert,_=certificate
    sink.mode='no_starttls';result=worker.run_one()
    assert result['state']=='failed' and result['test_stage']=='test_tls_unavailable' and sink.auth_count==0
    assert not sink.messages
    # A new fictional job shows a bad FAKE password is rejected after TLS.
    app.state.unified.book_court(player,DAY,13,1,1,[player]);sink.mode='accept'
    wrong=LoopbackSMTP(sink.server_address[1],cert.read_text(),sink.username,
                       'LG_FAKE_'+secrets.token_urlsafe(32),timeout=2)
    other=LoopbackSMTPWorker(app.state.engine.db,clock,wrong,sender=sink.username,app_origin='https://portal.example.invalid')
    r=other.run_one();assert r['state']=='failed' and r['test_stage']=='test_auth_rejected'
    assert sink.auth_count==1 and not sink.messages


def test_expired_lease_after_fake_acceptance_is_uncertain(smtp_world):
    app,owner,player,clock,sink,transport,_=smtp_world
    worker=LoopbackSMTPWorker(app.state.engine.db,clock,transport,sender=sink.username,
                              app_origin='https://portal.example.invalid',lease_seconds=5)
    sink.on_data=lambda:setattr(clock,'t',clock.t+6)
    result=worker.run_one()
    assert result['test_outcome']=='success' and result['state']=='uncertain' and len(sink.messages)==1
    assert worker.run_one() is None


def test_disabled_in_flight_after_fake_acceptance_is_uncertain(smtp_world):
    app,owner,player,clock,sink,transport,worker=smtp_world
    sink.on_data=lambda:app.state.accounts.disable_test_user(owner,player)
    result=worker.run_one()
    assert result['test_outcome']=='success' and result['state']=='uncertain' and len(sink.messages)==1
    with app.state.engine.db.read() as c:
        r=c.execute('SELECT state,failure_code FROM unified_test_mail_jobs').fetchone()
        assert tuple(r)==('uncertain','disabled_during_test_submission')
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_real_addresses_header_injection_and_real_secrets_are_refused(smtp_world,certificate):
    app,owner,player,clock,sink,transport,worker=smtp_world;cert,key=certificate
    with pytest.raises(ValueError):LoopbackSMTP(sink.server_address[1],cert.read_text(),'real@gmail.com',sink.password)
    with pytest.raises(ValueError):LoopbackSMTP(sink.server_address[1],cert.read_text(),sink.username,'real-password-should-never-be-entered')
    with app.state.engine.db.read() as c:job=c.execute('SELECT id FROM unified_test_mail_jobs').fetchone()[0]
    msg=render_test_mime(app.state.engine.db,job,'https://portal.example.invalid',sink.username)
    with pytest.raises(ValueError):transport.submit(msg,sender=sink.username,recipient='real@example.com')
    msg['Cc']='other@example.invalid'
    with pytest.raises(ValueError):transport.submit(msg,sender=sink.username,recipient='player@example.invalid')
    assert sink.auth_count==0 and not sink.messages
    # Invalid recipient in the database is failed before the transport opens a connection.
    with app.state.engine.db.tx() as c:c.execute("UPDATE unified_test_mail_jobs SET recipient='real@example.com'")
    assert worker.run_one() is None and sink.auth_count==0
    with app.state.engine.db.read() as c:assert c.execute('SELECT failure_code FROM unified_test_mail_jobs').fetchone()[0]=='invalid_test_recipient'


def test_gmail_adapter_uses_real_tls_wire_only_to_local_fake_sink(smtp_world,certificate,monkeypatch):
    import smtplib
    from lg_sports_deck.unified.gmail_test_sender import GmailTestSender,GmailTestSettings
    app,owner,player,clock,sink,transport,worker=smtp_world;cert,_=certificate
    # These are reserved example.com fixtures; the sink never relays mail.
    sender='temporary@example.com';recipient='allowlisted@example.com';password='not-a-real-gmail-password'
    sink.username=sender;sink.password=password;sink.allowed_mailboxes={sender,recipient}
    saved_factory=smtplib.SMTP;saved_connect=socket.create_connection
    def local_only(address,*args,**kwargs):
        if address[0]!='127.0.0.1':raise AssertionError('External SMTP is forbidden in this test')
        return saved_connect(address,*args,**kwargs)
    def redirected(host,port,timeout):
        assert (host,port)==('smtp.gmail.com',587)
        return saved_factory('127.0.0.1',sink.server_address[1],timeout=timeout)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(cadata=cert.read_text())
    monkeypatch.setattr(socket,'create_connection',local_only)
    monkeypatch.setattr(smtplib,'SMTP',redirected)
    monkeypatch.setattr(ssl,'create_default_context',lambda:context)
    sender_adapter=GmailTestSender(GmailTestSettings(sender,password,{recipient}))
    result=sender_adapter.send_one_test(recipient,explicit_send=True)
    assert result.outcome=='accepted' and result.stage=='smtp_accepted_not_inbox_delivery'
    assert sink.tls_count==sink.auth_count==1 and len(sink.messages)==1
    source,dest,wire=sink.messages[0];assert (source,dest)==(sender,recipient)
    msg=BytesParser(policy=policy.default).parsebytes(wire)
    assert msg['X-LG-Test-Only']=='yes' and 'No real booking' in msg.get_content()
    assert password not in wire.decode('utf-8')


def test_fake_verified_payment_creates_unsent_job_then_tls_sink_accepts_it(smtp_world,tmp_path):
    from lg_sports_deck.unified.payment_lab import FictionalGateway,FictionalPaymentLab
    app,owner,player,clock,sink,transport,worker=smtp_world;svc=app.state.unified
    sid=svc.create_session(owner,DAY,8,3,'Fictional payment-to-mail','fixture-test-token')
    fake=FictionalGateway(tmp_path/'separate-payment-fake.sqlite3',clock)
    lab=FictionalPaymentLab(svc,fake)
    attempt=lab.openplay(player,'test_integration_123456789',sid,[player])
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==1  # prior fictional rental
        assert c.execute('SELECT count(*) FROM session_registrations').fetchone()[0]==0
    fake.set_fixture(attempt['payment_id'],status='succeeded')
    payload,signature=fake.event(attempt['payment_id'],'fevt_integration_123456')
    assert lab.callback(payload,signature)['state']=='confirmed'
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM session_registrations').fetchone()[0]==1
    assert worker.run_one()['state']=='simulated'
    # Sender is configured at runtime and can be changed in the fake environment;
    # this does not establish that a real Gmail account permits an arbitrary alias.
    alternate=LoopbackSMTPWorker(app.state.engine.db,clock,transport,sender='temporary@example.invalid',
                                  app_origin='https://portal.example.invalid')
    assert alternate.run_one()['state']=='simulated'
    assert [m[0] for m in sink.messages]==['sender@example.invalid','temporary@example.invalid']
    assert [m[1] for m in sink.messages]==['player@example.invalid','player@example.invalid']
    last=BytesParser(policy=policy.default).parsebytes(sink.messages[-1][2])
    assert 'Fictional booking confirmation' in last['Subject']
    assert 'TEST ONLY' in last.get_content() and 'No real charge, booking or email' in last.get_content()


def test_queue_assignment_email_reaches_only_local_fake_tls_sink(smtp_world):
    """New queue email jobs reuse the fake transport, never real Gmail."""
    app,owner,player,clock,sink,transport,_=smtp_world;svc=app.state.unified;eng=app.state.engine
    players=[player]
    for i in range(3):
        pid=app.state.accounts.register_test_player(f'queue{i}@example.invalid',f'Queue {i}',PW)
        app.state.accounts.accept_test_notice(pid);players.append(pid)
    sid=svc.create_session(owner,DAY,8,3,'Fictional queue mail','queue-fixture-token')
    svc.book_openplay(player,sid,players)
    for pid in players:eng.check_in(sid,pid,'queue-fixture-token')
    mid=eng.assign_next(owner,sid,1)
    worker=LoopbackSMTPWorker(eng.db,clock,transport,sender='sender@example.invalid',
                              app_origin='https://portal.example.invalid',bucket='extra')
    result=worker.run_one()
    assert result['state']=='simulated' and result['test_stage']=='fake_sink_accepted'
    assert len(sink.messages)==1 and sink.messages[0][1]=='player@example.invalid'
    msg=BytesParser(policy=policy.default).parsebytes(sink.messages[0][2])
    assert msg['X-LG-Test-Only']=='yes' and 'match is assigned' in msg['Subject']
    assert f'/#match-{mid}' in msg.get_content() and 'No actual email was sent' in msg.get_content()
    with eng.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_test_extra_mail_receipts').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM unified_test_extra_mail_jobs WHERE state='queued'").fetchone()[0]==3
