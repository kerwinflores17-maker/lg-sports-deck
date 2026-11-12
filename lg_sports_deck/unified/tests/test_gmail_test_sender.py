"""Gmail adapter unit tests. SMTP is mocked: no external connection or email."""
from email import policy
from email.parser import BytesParser
import smtplib
import socket
import ssl

import pytest

from lg_sports_deck.unified.gmail_test_sender import (
    GmailTestSender,GmailTestSettings,TestSendingDisabled,
)


SENDER='temporary@example.com'
RECIPIENT='allowlisted@example.com'
SECRET='fake-credential-never-valid'


class FakeSMTP:
    def __init__(self,*,failure=None,starttls=True,auth=True):
        self.failure=failure;self.features={'starttls':starttls,'auth':auth};self.calls=[];self.raw=None
    def ehlo(self):self.calls.append('ehlo');return 250,b'OK'
    def has_extn(self,name):return self.features.get(name,False)
    def starttls(self,context):
        self.calls.append('starttls')
        assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED
        assert context.minimum_version>=ssl.TLSVersion.TLSv1_2
        if self.failure=='tls':raise ssl.SSLError('do not persist tls exception')
    def login(self,user,password):
        self.calls.append('login');assert user==SENDER and password==SECRET
        if self.failure=='auth':raise smtplib.SMTPAuthenticationError(535,b'do not persist password')
        if self.failure=='auth_temp':raise smtplib.SMTPAuthenticationError(454,b'temporary')
    def sendmail(self,sender,recipients,raw):
        self.calls.append('sendmail');self.raw=raw;assert sender==SENDER and recipients==[RECIPIENT]
        if self.failure=='rcpt_450':raise smtplib.SMTPRecipientsRefused({RECIPIENT:(450,b'temporary')})
        if self.failure=='rcpt_550':raise smtplib.SMTPRecipientsRefused({RECIPIENT:(550,b'permanent')})
        if self.failure=='data_450':raise smtplib.SMTPDataError(450,b'not accepted')
        if self.failure=='disconnect':raise smtplib.SMTPServerDisconnected('possibly accepted; no logging')
        return {}
    def quit(self):self.calls.append('quit')
    def close(self):self.calls.append('close')


@pytest.fixture
def settings():return GmailTestSettings(SENDER,SECRET,frozenset({RECIPIENT}))


@pytest.fixture(autouse=True)
def prohibit_actual_socket(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('External or loopback network forbidden in Gmail unit tests')
    monkeypatch.setattr(socket,'create_connection',forbidden)


def mock_smtp(monkeypatch,instance):
    def create(host,port,timeout):
        assert host=='smtp.gmail.com' and port==587 and timeout==10
        instance.calls.append('connect');return instance
    monkeypatch.setattr(smtplib,'SMTP',create)


def test_constructor_and_default_call_cannot_send_without_explicit_consent(settings,monkeypatch):
    def fail(*args,**kwargs):raise AssertionError('SMTP must not open')
    monkeypatch.setattr(smtplib,'SMTP',fail)
    sender=GmailTestSender(settings)
    with pytest.raises(TestSendingDisabled):sender.send_one_test(RECIPIENT)
    with pytest.raises(TestSendingDisabled):sender.send_one_test(RECIPIENT,explicit_send=1)
    with pytest.raises(ValueError):sender.send_one_test('unapproved@example.com',explicit_send=True)
    assert SECRET not in repr(settings) and SENDER not in repr(settings) and RECIPIENT not in repr(settings)


def test_sends_single_fixed_test_message_via_verified_tls_only(settings,monkeypatch):
    fake=FakeSMTP();mock_smtp(monkeypatch,fake)
    result=GmailTestSender(settings).send_one_test(RECIPIENT,explicit_send=True)
    assert result.outcome=='accepted' and result.stage=='smtp_accepted_not_inbox_delivery'
    assert fake.calls==['connect','ehlo','starttls','ehlo','login','sendmail','quit']
    message=BytesParser(policy=policy.default).parsebytes(fake.raw)
    assert message['From']==SENDER and message['To']==RECIPIENT
    assert not message.get('Cc') and not message.get('Bcc')
    assert message['Subject'].startswith('[TEST ONLY]') and message['X-LG-Test-Only']=='yes'
    assert 'No real booking, payment, match or video' in message.get_content()
    assert SECRET not in fake.raw.decode('utf-8') and SECRET not in repr(result)


@pytest.mark.parametrize('failure,outcome,stage',[
    ('tls','rejected','tls_or_auth_failed'),
    ('auth','rejected','authentication_rejected'),
    ('auth_temp','transient','authentication_rejected'),
    ('rcpt_450','transient','recipient_rejected'),
    ('rcpt_550','rejected','recipient_rejected'),
    ('data_450','transient','smtp_rejected'),
    ('disconnect','unknown','submission_uncertain'),
])
def test_failure_classification_never_claims_unknown_delivery(settings,monkeypatch,failure,outcome,stage):
    fake=FakeSMTP(failure=failure);mock_smtp(monkeypatch,fake)
    result=GmailTestSender(settings).send_one_test(RECIPIENT,explicit_send=True)
    assert (result.outcome,result.stage)==(outcome,stage)
    assert SECRET not in repr(result) and RECIPIENT not in repr(result)
    if failure in ('tls','auth','auth_temp'):assert 'sendmail' not in fake.calls
    if failure=='tls':assert 'login' not in fake.calls


def test_missing_starttls_or_auth_fails_before_credentials_or_email(settings,monkeypatch):
    no_tls=FakeSMTP(starttls=False);mock_smtp(monkeypatch,no_tls)
    assert GmailTestSender(settings).send_one_test(RECIPIENT,explicit_send=True).stage=='tls_unavailable'
    assert 'login' not in no_tls.calls and 'sendmail' not in no_tls.calls
    no_auth=FakeSMTP(auth=False);mock_smtp(monkeypatch,no_auth)
    assert GmailTestSender(settings).send_one_test(RECIPIENT,explicit_send=True).stage=='auth_unavailable'
    assert 'starttls' in no_auth.calls and 'login' not in no_auth.calls and 'sendmail' not in no_auth.calls


@pytest.mark.parametrize('address',[
    'sender@example.invalid','a@localhost','two@@example.com','bad\nBcc:x@example.com',
    'Name <a@example.com>','a..b@example.com','a@example..com','a@-bad.example.com',
    'a@example.test','é@example.com','a@example.c',
])
def test_invalid_or_reserved_addresses_rejected(address):
    with pytest.raises(ValueError):GmailTestSettings(address,SECRET,{RECIPIENT})
    with pytest.raises(ValueError):GmailTestSettings(SENDER,SECRET,{address})


def test_sender_and_allowlist_are_replaceable_not_hardcoded(settings,monkeypatch):
    other=GmailTestSettings('replacement@example.com','fake-other-credential-123',{'new@example.com'})
    assert settings.sender!=other.sender and settings.approved_recipients!=other.approved_recipients
    def fail(*args,**kwargs):raise AssertionError('SMTP must not open')
    monkeypatch.setattr(smtplib,'SMTP',fail)
    with pytest.raises(ValueError):GmailTestSender(other).send_one_test(RECIPIENT,explicit_send=True)
