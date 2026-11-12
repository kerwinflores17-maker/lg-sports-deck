"""Offline fictional payment laboratory regression; no external provider/money."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
import secrets
import sqlite3

import pytest

from lg_sports_deck.core.domain import Conflict, PermissionDenied
from lg_sports_deck.unified.app import create_app
from lg_sports_deck.unified.domain import MANILA
from lg_sports_deck.unified.payment_lab import FictionalGateway, FictionalPaymentLab


PW='A fictional test password 47'
DAY=date(2027,6,2)


class Clock:
    def __init__(self):self.t=datetime(2027,6,2,10,0,tzinfo=MANILA).timestamp()
    def __call__(self):return self.t


@pytest.fixture
def world(tmp_path):
    clock=Clock()
    app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'media',{'https://testserver'},clock=clock)
    app.state.owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Fictional Owner',PW)
    app.state.clock=clock
    app.state.unified.set_limits(app.state.owner,4,12)
    fake=FictionalGateway(tmp_path/'separate-fake-gateway.sqlite3',clock)
    lab=FictionalPaymentLab(app.state.unified,fake)
    yield app,fake,lab
    app.state.recorder.close()


def players(app,n):
    result=[]
    for i in range(n):
        uid=app.state.accounts.register_test_player(f'lab{i}@example.invalid',f'Lab Player {i}',PW)
        app.state.accounts.accept_test_notice(uid);result.append(uid)
    return result


def session(app,*,start=8,duration=3):
    return app.state.unified.create_session(app.state.owner,DAY,start,duration,'Fictional session','test-token-for-lab')


def key(label):
    return 'test_'+label.ljust(16,'_')


def evt(fake,payment_id,label,*,timestamp=None):
    return fake.event(payment_id,'fevt_'+label.ljust(12,'_'),timestamp=timestamp)


def count(db,table):
    with db.read() as c:return c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


def test_pending_quotes_do_not_book_or_send_and_key_is_bound(world,monkeypatch):
    import socket
    app,fake,lab=world;people=players(app,2);sid=session(app)
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network forbidden')))
    a=lab.openplay(people[0],key('first'),sid,people)
    again=lab.openplay(people[0],key('first'),sid,list(reversed(people)))
    assert a==again and a['state']=='awaiting' and a['amount_minor']==40000 and a['currency']=='PHP'
    assert a['booking_id'] is None and a['mode']=='fictional-only'
    with pytest.raises(Conflict):lab.openplay(people[0],key('first'),sid,[people[0]])
    with pytest.raises(ValueError):lab.openplay(people[0],'not-a-test-key',sid,people)
    with pytest.raises(ValueError):lab.openplay(people[0],key('invalid'),sid,[people[0],people[0]])
    assert count(app.state.engine.db,'unified_bookings')==0
    assert count(app.state.engine.db,'session_registrations')==0
    assert count(app.state.engine.db,'unified_test_mail_jobs')==0
    assert app.state.unified.sales(app.state.owner,'daily')['total']==0
    assert fake.retrieve(a['payment_id']).status=='pending'


def test_callback_authentication_dedup_and_untrusted_success_claims(world):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    a=lab.openplay(p,key('auth'),sid,[p]);raw,sig=evt(fake,a['payment_id'],'same')
    with pytest.raises(PermissionDenied):lab.callback(raw,'0'*64)
    with pytest.raises(PermissionDenied):lab.callback(raw+b' ','0'*64)
    with pytest.raises(PermissionDenied):lab.callback(b'x'*4097,'0'*64)
    old=evt(fake,a['payment_id'],'old',timestamp=int(fake.clock())-301)
    with pytest.raises(PermissionDenied):lab.callback(*old)
    future=evt(fake,a['payment_id'],'future',timestamp=int(fake.clock())+301)
    with pytest.raises(PermissionDenied):lab.callback(*future)
    # A correctly signed callback is only a nudge. Pending provider state is not payment.
    assert lab.callback(raw,sig)['state']=='awaiting'
    assert lab.callback(raw,sig)['state']=='awaiting'
    assert count(app.state.engine.db,'unified_test_payment_events')==1
    changed=evt(fake,'fpay_'+secrets.token_hex(16),'same')
    with pytest.raises(Conflict):lab.callback(*changed)
    assert count(app.state.engine.db,'unified_bookings')==0
    # Unknown, validly signed fictional reference cannot create a booking.
    unknown=evt(fake,'fpay_'+secrets.token_hex(16),'unknown')
    assert lab.callback(*unknown)['state']=='unknown_payment'
    assert count(app.state.engine.db,'unified_bookings')==0
    assert lab.inspect(p,a['intent_id'])['state']=='awaiting'


def test_success_is_idempotent_atomic_and_only_after_fake_confirmation(world):
    app,fake,lab=world;people=players(app,2);sid=session(app);db=app.state.engine.db
    a=lab.openplay(people[0],key('success'),sid,people)
    fake.set_fixture(a['payment_id'],status='succeeded')
    assert lab.inspect(people[0],a['intent_id'])['state']=='awaiting'  # no client redirect confirmation
    e=evt(fake,a['payment_id'],'success')
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:lab.callback(*e),range(4)))
    assert {x['state'] for x in results}=={'confirmed'}
    assert len({x['booking_id'] for x in results})==1
    assert lab.callback(*evt(fake,a['payment_id'],'different'))['booking_id']==results[0]['booking_id']
    assert count(db,'unified_bookings')==1 and count(db,'session_registrations')==2
    assert count(db,'unified_test_mail_jobs')==1  # unsent booker job from same transaction
    with db.read() as c:
        booking=c.execute('SELECT * FROM unified_bookings').fetchone()
        assert booking['amount']==400 and booking['state']=='simulated_paid'
        assert c.execute('SELECT state FROM unified_test_mail_jobs').fetchone()[0]=='queued'
    assert app.state.unified.sales(app.state.owner,'daily')['total']==400


def test_gateway_outage_missed_callback_and_restart_reconcile(world):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    a=lab.openplay(p,key('outage'),sid,[p]);fake.set_fixture(a['payment_id'],status='succeeded')
    signed=evt(fake,a['payment_id'],'missed');fake.unavailable=True
    status=lab.callback(*signed)
    assert status['state']=='awaiting' and status['reason_code']=='gateway_unavailable'
    assert count(app.state.engine.db,'unified_bookings')==0
    fake.unavailable=False
    # A different in-memory callback key models a restart; old callbacks fail
    # verification but the fake provider's separate durable record can be polled.
    restarted=FictionalGateway(fake.path,app.state.clock)
    restarted_lab=FictionalPaymentLab(app.state.unified,restarted)
    with pytest.raises(PermissionDenied):restarted_lab.callback(*signed)
    result=restarted_lab.reconcile_pending()
    assert len(result)==1 and result[0]['state']=='confirmed'
    assert count(app.state.engine.db,'unified_bookings')==1


def test_initialization_failure_retries_idempotently_with_no_fake_double_charge(world):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    fake.unavailable=True;a=lab.openplay(p,key('initial'),sid,[p])
    assert a['state']=='initializing' and a['payment_id'] is None and a['reason_code']=='gateway_unavailable'
    fake.unavailable=False
    b=lab.openplay(p,key('initial'),sid,[p])
    assert b['intent_id']==a['intent_id'] and b['state']=='awaiting' and b['payment_id']
    assert lab.openplay(p,key('initial'),sid,[p])==b
    with sqlite3.connect(fake.path) as c:assert c.execute('SELECT count(*) FROM fictional_gateway_payments').fetchone()[0]==1
    assert count(app.state.engine.db,'unified_bookings')==0


def test_concurrent_same_checkout_key_creates_one_fake_intent_and_payment(world):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows=list(pool.map(lambda _:lab.openplay(p,key('parallel'),sid,[p]),range(12)))
    assert len({r['intent_id'] for r in rows})==len({r['payment_id'] for r in rows})==1
    assert count(app.state.engine.db,'unified_test_payment_intents')==1
    with sqlite3.connect(fake.path) as c:assert c.execute('SELECT count(*) FROM fictional_gateway_payments').fetchone()[0]==1


def test_waitlist_race_never_oversells_and_loser_paid_is_not_hidden(world):
    app,fake,lab=world;people=players(app,6);sid=session(app);svc=app.state.unified;db=app.state.engine.db
    svc.book_openplay(people[0],sid,people[:4])
    svc.join_waitlist(people[4],sid);svc.join_waitlist(people[5],sid)
    assert svc.mark_no_show(app.state.owner,sid,people[0])==2
    intents=[lab.openplay(people[i],key(f'wait{i}'),sid,[people[i]]) for i in (4,5)]
    for a in intents:fake.set_fixture(a['payment_id'],status='succeeded')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda a:lab.reconcile(a['intent_id']),intents))
    assert sorted(r['state'] for r in results)==['confirmed','paid_needs_review']
    loser=next(r for r in results if r['state']=='paid_needs_review')
    assert loser['needs_review'] and loser['booking_id'] is None and loser['reason_code']=='booking_unavailable_or_changed'
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(sid,)).fetchone()[0]==4
        assert c.execute('SELECT attendance FROM unified_booking_players WHERE user_id=?',(people[0],)).fetchone()[0]=='no_show'
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM unified_test_payment_intents WHERE needs_review=1').fetchone()[0]==1
    # This is first processed FAKE success, not a claim about real chronological payment ordering.
    assert svc.sales(app.state.owner,'daily')['total']==1000


def test_simultaneous_fictional_rental_payments_do_not_double_book(world):
    app,fake,lab=world;people=players(app,2)
    intents=[lab.court(p,key(f'court{i}'),DAY,12,2,1,[p]) for i,p in enumerate(people)]
    assert all(r['amount_minor']==50000 for r in intents)
    for a in intents:fake.set_fixture(a['payment_id'],status='succeeded')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda x:lab.reconcile(x['intent_id']),intents))
    assert sorted(r['state'] for r in results)==['confirmed','paid_needs_review']
    assert count(app.state.engine.db,'unified_bookings')==1


@pytest.mark.parametrize('corruption', ['amount','currency','reference'])
def test_mismatched_provider_details_are_quarantined(world,corruption):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    a=lab.openplay(p,key('corrupt'),sid,[p]);kw={
        'amount':{'amount_minor':19999},'currency':{'currency':'USD'},
        'reference':{'reference':'pi_test_'+'0'*32}}[corruption]
    fake.set_fixture(a['payment_id'],status='succeeded',**kw)
    r=lab.callback(*evt(fake,a['payment_id'],'corrupt'))
    assert r['state']=='investigate' and r['needs_review'] and r['reason_code']=='provider_details_mismatch'
    assert r['booking_id'] is None and count(app.state.engine.db,'unified_bookings')==0
    # A later change never silently clears the money-related review flag.
    fake.set_fixture(a['payment_id'],status='succeeded',amount_minor=20000,currency='PHP',reference=a['intent_id'])
    assert lab.reconcile(a['intent_id'])['state']=='investigate'
    assert count(app.state.engine.db,'unified_bookings')==0


def test_changed_price_and_disabled_companion_become_review_not_fake_booking(world):
    app,fake,lab=world;people=players(app,2);sid=session(app);svc=app.state.unified
    a=lab.openplay(people[0],key('price'),sid,[people[0]])
    svc.set_rates(app.state.owner,300,250,50)
    fake.set_fixture(a['payment_id'],status='succeeded')
    result=lab.reconcile(a['intent_id'])
    assert result['state']=='paid_needs_review' and result['booking_id'] is None
    b=lab.openplay(people[0],key('disabled'),sid,people)
    app.state.accounts.disable_test_user(app.state.owner,people[1])
    fake.set_fixture(b['payment_id'],status='succeeded')
    assert lab.reconcile(b['intent_id'])['state']=='paid_needs_review'
    assert count(app.state.engine.db,'unified_bookings')==0
    assert count(app.state.engine.db,'unified_test_mail_jobs')==0


def test_mutated_intent_payload_never_silently_changes_a_paid_booking(world):
    app,fake,lab=world;people=players(app,2);sid=session(app);db=app.state.engine.db
    a=lab.openplay(people[0],key('mutation'),sid,[people[0]])
    with db.tx() as c:
        c.execute("UPDATE unified_test_payment_intents SET request_json=? WHERE id=?",
                  (json.dumps({'kind':'openplay','session_id':sid,'players':[people[1]]}),a['intent_id']))
    fake.set_fixture(a['payment_id'],status='succeeded')
    r=lab.reconcile(a['intent_id'])
    assert r['state']=='investigate' and r['reason_code']=='intent_data_changed'
    assert count(db,'unified_bookings')==0 and count(db,'session_registrations')==0


def test_booking_failure_rolls_back_fictional_booking_and_unsent_job(world,monkeypatch):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app);svc=app.state.unified
    a=lab.openplay(p,key('rollback'),sid,[p]);fake.set_fixture(a['payment_id'],status='succeeded')
    original=svc._book_openplay_in_tx
    def fail_after_insert(*args,**kwargs):
        original(*args,**kwargs)
        raise Conflict('Fictional failure after booking and job creation')
    monkeypatch.setattr(svc,'_book_openplay_in_tx',fail_after_insert)
    result=lab.reconcile(a['intent_id'])
    assert result['state']=='paid_needs_review'
    assert count(app.state.engine.db,'unified_bookings')==0
    assert count(app.state.engine.db,'session_registrations')==0
    assert count(app.state.engine.db,'unified_test_mail_jobs')==0
    assert count(app.state.engine.db,'unified_test_payment_intents')==1


def test_failed_then_late_paid_and_reversal_require_manual_review(world):
    app,fake,lab=world;people=players(app,2);sid=session(app)
    a=lab.openplay(people[0],key('late'),sid,[people[0]])
    fake.set_fixture(a['payment_id'],status='failed')
    assert lab.reconcile(a['intent_id'])['state']=='failed'
    fake.set_fixture(a['payment_id'],status='succeeded')
    later=lab.reconcile(a['intent_id'])
    assert later['state']=='paid_needs_review' and later['reason_code']=='paid_after_failure'
    b=lab.openplay(people[1],key('reversal'),sid,[people[1]])
    fake.set_fixture(b['payment_id'],status='succeeded')
    confirmed=lab.reconcile(b['intent_id']);assert confirmed['state']=='confirmed'
    fake.set_fixture(b['payment_id'],status='reversed')
    reversed_=lab.reconcile(b['intent_id'])
    assert reversed_['state']=='confirmed' and reversed_['needs_review'] and reversed_['booking_id']==confirmed['booking_id']
    assert reversed_['reason_code']=='provider_status_changed'
    fake.set_fixture(b['payment_id'],status='succeeded')
    assert lab.reconcile(b['intent_id'])['needs_review'] is True
    assert count(app.state.engine.db,'unified_bookings')==1


def test_stale_fake_snapshot_cannot_undo_or_falsely_flag_a_booking(world):
    app,fake,lab=world;p=players(app,1)[0];sid=session(app)
    a=lab.openplay(p,key('stale'),sid,[p]);stale=fake.retrieve(a['payment_id'])
    fake.set_fixture(a['payment_id'],status='succeeded');confirmed=lab.reconcile(a['intent_id'])
    assert confirmed['state']=='confirmed' and not confirmed['needs_review']
    r=lab._apply_snapshot(a['intent_id'],stale)
    assert r['state']=='confirmed' and not r['needs_review'] and r['booking_id']==confirmed['booking_id']


def test_checkout_view_private_and_real_addresses_refused(world):
    app,fake,lab=world;people=players(app,2);sid=session(app);db=app.state.engine.db
    a=lab.openplay(people[0],key('private'),sid,[people[0]])
    with pytest.raises(PermissionDenied):lab.inspect(people[1],a['intent_id'])
    assert lab.inspect(app.state.owner,a['intent_id'])['payment_id']==a['payment_id']
    with db.tx() as c:c.execute("UPDATE users SET email='real@example.com' WHERE id=?",(people[0],))
    fake.set_fixture(a['payment_id'],status='succeeded')
    result=lab.reconcile(a['intent_id'])
    assert result['state']=='paid_needs_review' and count(db,'unified_bookings')==0
    with pytest.raises(PermissionDenied):lab.openplay(people[0],key('real'),sid,[people[0]])


def test_unknown_gateway_database_is_never_reseeded_and_separate_db_required(world,tmp_path):
    app,fake,lab=world
    path=tmp_path/'unknown.sqlite3'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE important_data(value TEXT)');c.execute("INSERT INTO important_data VALUES('preserve')")
    with pytest.raises(ValueError):FictionalGateway(path,app.state.clock)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT value FROM important_data').fetchone()[0]=='preserve'
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='fictional_gateway_identity'").fetchone()[0]==0
    with pytest.raises(ValueError):FictionalGateway(app.state.engine.db.path,app.state.clock)
    # No vendor selection or real adapter can be passed into this lab.
    with pytest.raises(ValueError):FictionalPaymentLab(app.state.unified,object())
