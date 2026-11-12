"""Fake-only manual QR review laboratory; no real payments or networks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
import socket
import smtplib

import pytest

from lg_sports_deck.core.domain import Conflict,PermissionDenied
from lg_sports_deck.unified.manual_qr_lab import FictionalManualQR
from lg_sports_deck.unified.tests.test_unified import world, client, login, create_staff, grants, fixture_players


DAY=date(2027,6,2)


def offline(monkeypatch):
    def fail(*a,**kw):raise AssertionError('No real network or email permitted')
    monkeypatch.setattr(socket,'create_connection',fail)
    monkeypatch.setattr(smtplib,'SMTP',fail)
    monkeypatch.setattr(smtplib,'SMTP_SSL',fail)


def openplay(world):
    svc=world.state.unified;people=fixture_players(world,5)
    sid=svc.create_session(world.state.owner,DAY,8,3,'Fictional manual test','fixture-token-qr')
    return sid,people


def test_manual_openplay_stays_pending_until_superuser_account_check_then_emails_unsent(world,monkeypatch):
    offline(monkeypatch);sid,p=openplay(world);svc=world.state.unified;lab=FictionalManualQR(svc);owner=world.state.owner
    settings=world.state.test_integration_settings
    r=settings.view(owner);settings.replace_booking_recipient(owner,r['revision'],'venue@example.invalid')
    payload={'session_id':sid,'player_ids':p[:4]}
    intent=lab.initiate(p[0],'fake_key_1234567890','openplay',payload)
    assert intent['state']=='pending' and intent['amount']==800 and intent['booking_id'] is None
    assert lab.initiate(p[0],'fake_key_1234567890','openplay',payload)==intent
    with pytest.raises(Conflict):lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0
    with pytest.raises(ValueError):lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-abcdefgh',800)  # no checkbox
    with pytest.raises(ValueError):lab.confirm_account_check(owner,intent['intent_id'],'12345678901',800,account_checked=True)  # not fictional
    result=lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-abcdefgh',800,account_checked=True)
    assert result['state']=='confirmed' and result['booking_id']
    assert lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-abcdefgh',800,account_checked=True)==result
    with world.state.engine.db.read() as c:
        jobs=[dict(r) for r in c.execute('SELECT * FROM unified_test_mail_jobs ORDER BY id')]
        assert len(jobs)==2 and {j['recipient'] for j in jobs}=={'p0@example.invalid','venue@example.invalid'}
        assert all(j['state']=='queued' for j in jobs)
        assert c.execute('SELECT count(*) FROM unified_booking_players').fetchone()[0]==4
        raw=str(c.execute('SELECT reference_hash FROM unified_test_manual_qr_intents').fetchone()[0])
        assert 'FAKE-QR' not in raw and len(raw)==64


def test_manual_roles_and_superuser_delegation_are_rechecked(world):
    sid,p=openplay(world);svc=world.state.unified;lab=FictionalManualQR(svc);owner=world.state.owner
    intent=lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    with pytest.raises(PermissionDenied):lab.read(p[1],intent['intent_id'])
    assert lab.read(p[0],intent['intent_id'])['state']=='pending'
    with ExitStack() as stack:
        oc=stack.enter_context(client(world));oh=login(oc,'owner@example.invalid')
        admin=create_staff(oc,oh,'admin@example.invalid','admin')
        manager=create_staff(oc,oh,'manager@example.invalid','manager')
        ac=stack.enter_context(client(world));ah=login(ac,'admin@example.invalid')
        assert 'payments.manual_verify' in oc.get('/api/staff').json()['grantable']
        for actor in (admin,manager,p[0]):
            with pytest.raises(PermissionDenied):lab.confirm_account_check(actor,intent['intent_id'],'FAKE-QR-abcdefgh',200,account_checked=True)
            with pytest.raises(PermissionDenied):lab.review_details(actor,intent['intent_id'])
        assert ac.post(f'/api/staff/{admin}/permissions',headers=ah,json={'permissions':['payments.manual_verify']}).status_code==403
        assert grants(oc,oh,admin,['payments.manual_verify']).status_code==200
        details=lab.review_details(admin,intent['intent_id'])
        assert details['test_booker_id']==p[0] and details['test_request']['session_id']==sid
        assert lab.read(admin,intent['intent_id'])['state']=='pending'
        assert lab.confirm_account_check(admin,intent['intent_id'],'FAKE-QR-abcdefgh',200,account_checked=True)['state']=='confirmed'
        assert grants(oc,oh,admin,[]).status_code==200
        second=lab.initiate(p[1],'fake_key_2234567890','openplay',{'session_id':sid,'player_ids':[p[1]]})
        with pytest.raises(PermissionDenied):lab.confirm_account_check(admin,second['intent_id'],'FAKE-QR-nextgood',200,account_checked=True)
        with pytest.raises(PermissionDenied):lab.read(admin,second['intent_id'])
        assert grants(oc,oh,manager,['payments.manual_verify']).status_code==200
        assert lab.confirm_account_check(manager,second['intent_id'],'FAKE-QR-nextgood',200,account_checked=True)['state']=='confirmed'
        third=lab.initiate(p[2],'fake_key_3234567890','openplay',{'session_id':sid,'player_ids':[p[2]]})
        assert grants(oc,oh,manager,[]).status_code==200
        with pytest.raises(PermissionDenied):lab.confirm_account_check(manager,third['intent_id'],'FAKE-QR-thirdone',200,account_checked=True)
        with pytest.raises(PermissionDenied):lab.read(manager,third['intent_id'])
        assert grants(oc,oh,manager,['payments.manual_verify']).status_code==200
        world.state.accounts.disable_test_user(owner,manager)
        with pytest.raises(PermissionDenied):lab.confirm_account_check(manager,third['intent_id'],'FAKE-QR-thirdone',200,account_checked=True)
        assert lab.read(owner,third['intent_id'])['state']=='pending'


def test_amount_mismatch_and_duplicate_reference_go_to_safe_review(world):
    sid,p=openplay(world);lab=FictionalManualQR(world.state.unified);owner=world.state.owner
    one=lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    two=lab.initiate(p[1],'fake_key_2234567890','openplay',{'session_id':sid,'player_ids':[p[1]]})
    wrong=lab.confirm_account_check(owner,one['intent_id'],'FAKE-QR-badamount',199,account_checked=True)
    assert wrong['state']=='manual_review' and wrong['issue']=='amount_mismatch' and wrong['booking_id'] is None
    assert lab.confirm_account_check(owner,one['intent_id'],'FAKE-QR-badamount',199,account_checked=True)==wrong
    with pytest.raises(Conflict):lab.confirm_account_check(owner,two['intent_id'],'FAKE-QR-badamount',200,account_checked=True)
    assert lab.read(p[1],two['intent_id'])['state']=='pending'
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0


def test_concurrent_manual_review_never_oversells_or_discards_second_fake_payment(world):
    people=fixture_players(world,2);svc=world.state.unified;lab=FictionalManualQR(svc);owner=world.state.owner
    data=lambda p:{'day':DAY.isoformat(),'start_hour':12,'duration':2,'court':1,'player_ids':[p]}
    a=lab.initiate(people[0],'fake_key_1234567890','court',data(people[0]))
    b=lab.initiate(people[1],'fake_key_2234567890','court',data(people[1]))
    def verify(args):return lab.confirm_account_check(owner,args[0],args[1],500,account_checked=True)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(verify,[(a['intent_id'],'FAKE-QR-unique_1'),(b['intent_id'],'FAKE-QR-unique_2')]))
    assert sorted(r['state'] for r in results)==['confirmed','manual_review']
    assert next(r for r in results if r['state']=='manual_review')['issue']=='paid_but_unbookable_or_changed'
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_test_manual_qr_intents WHERE reference_hash IS NOT NULL').fetchone()[0]==2


def test_duplicate_review_is_atomic_and_review_can_reject_fictional_payment(world):
    sid,p=openplay(world);lab=FictionalManualQR(world.state.unified);owner=world.state.owner
    intent=lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    def verify(_):return lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-concurrent',200,account_checked=True)
    with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(verify,range(4)))
    assert all(r==result[0] and r['state']=='confirmed' for r in result)
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==1
    two=lab.initiate(p[1],'fake_key_2234567890','openplay',{'session_id':sid,'player_ids':[p[1]]})
    rejected=lab.reject(owner,two['intent_id']);assert rejected['state']=='rejected' and lab.reject(owner,two['intent_id'])==rejected
    with pytest.raises(Conflict):lab.confirm_account_check(owner,two['intent_id'],'FAKE-QR-another1',200,account_checked=True)


def test_price_change_after_fake_payment_requires_review(world):
    people=fixture_players(world,1);svc=world.state.unified;lab=FictionalManualQR(svc);owner=world.state.owner
    payload={'day':DAY.isoformat(),'start_hour':12,'duration':2,'court':1,'player_ids':people}
    intent=lab.initiate(people[0],'fake_key_1234567890','court',payload);assert intent['amount']==500
    svc.set_rates(owner,200,300,50)
    result=lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-pricechanged',500,account_checked=True)
    assert result['state']=='manual_review' and result['booking_id'] is None
    with world.state.engine.db.read() as c:assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0


def test_unexpected_database_failure_rolls_back_booking_email_and_review(world):
    import sqlite3
    sid,p=openplay(world);lab=FictionalManualQR(world.state.unified);eng=world.state.engine;owner=world.state.owner
    intent=lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    with eng.db.tx() as c:
        c.execute("""CREATE TRIGGER force_test_failure AFTER INSERT ON unified_booking_players
                     BEGIN SELECT RAISE(ABORT,'forced test write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError,match='forced test write failure'):
        lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-dbfailure',200,account_checked=True)
    with eng.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0
        row=c.execute('SELECT state,reference_hash FROM unified_test_manual_qr_intents').fetchone()
        assert row['state']=='pending' and row['reference_hash'] is None
    with eng.db.tx() as c:c.execute('DROP TRIGGER force_test_failure')
    assert lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-dbfailure',200,account_checked=True)['state']=='confirmed'


def test_manual_qr_lab_is_not_mounted_as_a_live_route(world):
    FictionalManualQR(world.state.unified)
    with client(world) as c:
        h=login(c,'owner@example.invalid')
        assert c.post('/api/u/manual-qr/confirm',headers=h,json={}).status_code==404


def test_payment_of_disabled_booker_is_not_lost_or_confirmed(world):
    sid,p=openplay(world);lab=FictionalManualQR(world.state.unified);owner=world.state.owner
    intent=lab.initiate(p[0],'fake_key_1234567890','openplay',{'session_id':sid,'player_ids':[p[0]]})
    world.state.accounts.disable_test_user(owner,p[0])
    result=lab.confirm_account_check(owner,intent['intent_id'],'FAKE-QR-disabled1',200,account_checked=True)
    assert result['state']=='manual_review' and result['booking_id'] is None
    with world.state.engine.db.read() as c:
        row=c.execute('SELECT reference_hash FROM unified_test_manual_qr_intents WHERE id=?',(intent['intent_id'],)).fetchone()
        assert row['reference_hash'] and c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
