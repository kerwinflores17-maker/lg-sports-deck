"""Fictional venue-inbox settings and unsent paid-booking notices only.

These tests use invented .invalid accounts, a private SQLite fixture and an
unmounted fake payment gateway. They never contact Gmail or receive money.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
import smtplib
import socket
import sqlite3

import pytest

from lg_sports_deck.core.domain import Conflict
from lg_sports_deck.unified.notifications import install_test_schema, render_test_mime
from lg_sports_deck.unified.payment_lab import FictionalGateway, FictionalPaymentLab
from lg_sports_deck.unified.tests.test_unified import (
    world, client, login, register, create_staff, grants, fixture_players,
)


SETTINGS='/api/u/test-integration-settings'
RECIPIENT=SETTINGS+'/booking-recipient'
VENUE_NOTICES='/api/u/test-venue-booking-notices'
DAY=date(2027,6,3)


def no_network(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('External/network transport must not be used')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(smtplib,'SMTP',forbidden)
    monkeypatch.setattr(smtplib,'SMTP_SSL',forbidden)


def jobs(world):
    with world.state.engine.db.read() as c:
        return [dict(r) for r in c.execute('SELECT * FROM unified_test_mail_jobs ORDER BY id')]


def venue_jobs(world):
    return [r for r in jobs(world) if r['event_key'].startswith('venue_booking:')]


def test_recipient_is_superuser_only_separate_and_rejects_real_addresses(world,monkeypatch):
    no_network(monkeypatch)
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));admin=stack.enter_context(client(world))
        manager=stack.enter_context(client(world));player=stack.enter_context(client(world))
        assert owner.get(VENUE_NOTICES).status_code==401
        assert owner.post(RECIPIENT,json={'expected_revision':0,'recipient_email':'venue@example.invalid'}).status_code==403  # origin/CSRF rejected first
        oh=login(owner,'owner@example.invalid')
        aid=create_staff(owner,oh,'admin@example.invalid','admin')
        mid=create_staff(owner,oh,'manager@example.invalid','manager')
        allowed=owner.get('/api/staff').json()['grantable']
        assert grants(owner,oh,aid,allowed).status_code==200
        assert grants(owner,oh,mid,allowed).status_code==200
        ah=login(admin,'admin@example.invalid');mh=login(manager,'manager@example.invalid')
        ph,_=register(player,'player@example.invalid','Fictional Player')
        for c,h in ((admin,ah),(manager,mh),(player,ph)):
            assert c.get(VENUE_NOTICES,headers={'X-Role':'superuser'}).status_code==403
            assert c.post(RECIPIENT,headers=h,json={'expected_revision':0,'recipient_email':'venue@example.invalid'}).status_code==403
        initial=owner.get(SETTINGS)
        assert initial.json()['booking_recipient_email'] is None
        assert initial.json()['sender_email'] is None
        empty=owner.get(VENUE_NOTICES)
        assert empty.status_code==200 and empty.headers['cache-control']=='no-store' and empty.json()['notices']==[]
        data={'expected_revision':0,'recipient_email':'venue@example.invalid'}
        assert owner.post(RECIPIENT,headers={'Origin':'https://attacker.invalid','X-CSRF-Token':oh['X-CSRF-Token']},json=data).status_code==403
        assert owner.post(RECIPIENT,headers={'Origin':oh['Origin']},json=data).status_code==403
        for invalid in ('real@gmail.com','venue@example.com','not-an-email','venue@example.invalid\nBcc:other@example.invalid'):
            assert owner.post(RECIPIENT,headers=oh,json={**data,'recipient_email':invalid}).status_code==422
        assert owner.post(RECIPIENT,headers=oh,json={**data,'credential':'never-accepted'}).status_code==422
        assert owner.post(RECIPIENT,headers=oh,json={**data,'expected_revision':True}).status_code==422
        saved=owner.post(RECIPIENT,headers=oh,json={'expected_revision':0,'recipient_email':'  Venue@EXAMPLE.INVALID  '})
        assert saved.status_code==200 and saved.headers['cache-control']=='no-store'
        assert saved.json()['booking_recipient_email']=='venue@example.invalid'
        assert saved.json()['sender_email'] is None and saved.json()['revision']==1
        # Sender and venue destination have separate values but share a revision.
        sender=owner.post(SETTINGS+'/sender',headers=oh,json={'expected_revision':1,'sender_email':'sender@example.invalid'})
        assert sender.status_code==200 and sender.json()['booking_recipient_email']=='venue@example.invalid'
        assert sender.json()['sender_email']=='sender@example.invalid' and sender.json()['revision']==2
        assert owner.post(RECIPIENT,headers=oh,json={'expected_revision':1,'recipient_email':'other@example.invalid'}).status_code==409
        unchanged=owner.post(RECIPIENT,headers=oh,json={'expected_revision':2,'recipient_email':'venue@example.invalid'})
        assert unchanged.status_code==200 and unchanged.json()['revision']==2
        cleared=owner.post(RECIPIENT,headers=oh,json={'expected_revision':2,'recipient_email':''})
        assert cleared.status_code==200 and cleared.json()['revision']==3
        assert cleared.json()['booking_recipient_email'] is None and cleared.json()['sender_email']=='sender@example.invalid'
        with world.state.engine.db.read() as c:
            audit=[dict(r) for r in c.execute("SELECT * FROM auth_audit WHERE event='unified.test_booking_recipient_changed' ORDER BY id")]
            dest=c.execute('SELECT * FROM unified_test_booking_notice_destination').fetchone()
        assert len(audit)==2 and all(row['actor_id']==world.state.owner for row in audit)
        assert 'venue@example.invalid' not in str(audit) and 'never-accepted' not in str(audit)
        assert dest['recipient_email'] is None and dest['owner_user_id'] is None


def test_new_jobs_only_snapshots_and_player_notice_stays_independent(world,monkeypatch):
    no_network(monkeypatch)
    svc=world.state.unified;settings=world.state.test_integration_settings;owner=world.state.owner
    p0,p1=fixture_players(world,2)
    sid=svc.create_session(owner,DAY,6,3,'Fictional session','test-token-session')
    # Earlier paid test booking gets the existing player job only, never backfilled.
    before=svc.book_openplay(p0,sid,[p0]);assert len(jobs(world))==1 and not venue_jobs(world)
    a=settings.replace_booking_recipient(owner,0,'venue-a@example.invalid')
    after=svc.book_openplay(p1,sid,[p1])
    rental_a=svc.book_court(p0,DAY,10,2,2,[p0])
    assert len(venue_jobs(world))==2
    settings.replace_sender(owner,a['revision'],'sender@example.invalid')
    b=settings.replace_booking_recipient(owner,a['revision']+1,'venue-b@example.invalid')
    rental_b=svc.book_court(p0,DAY,12,1,2,[p0])
    settings.replace_booking_recipient(owner,b['revision'],'')
    disabled=svc.book_court(p0,DAY,13,1,2,[p0])
    install_test_schema(world.state.engine.db)  # repeat/upgrade does not backfill or duplicate
    all_jobs=jobs(world);venues=venue_jobs(world)
    assert len(all_jobs)==5+3 and len(venues)==3
    assert [v['recipient'] for v in venues]==['venue-a@example.invalid','venue-a@example.invalid','venue-b@example.invalid']
    assert [v['event_key'] for v in venues]==[f"venue_booking:{after['booking_id']}",f"venue_booking:{rental_a['booking_id']}",f"venue_booking:{rental_b['booking_id']}"]
    assert all(v['state']=='queued' and v['user_id']==owner and 'TEST ONLY' in v['subject'] for v in venues)
    assert 'Open Play' in venues[0]['body'] and 'PHP 200' in venues[0]['body']
    assert 'court rental' in venues[1]['body'] and 'Court 02' in venues[1]['body'] and 'PHP 500' in venues[1]['body']
    mime=render_test_mime(world.state.engine.db,venues[1]['id'],'https://deck.example.invalid','sender@example.invalid')
    assert mime['To']=='venue-a@example.invalid' and mime['From']=='sender@example.invalid'
    assert mime['X-LG-Test-Only']=='yes' and 'No real payment' in mime.get_content()
    assert all(v['event_key'] not in (f"venue_booking:{before['booking_id']}",f"venue_booking:{disabled['booking_id']}") for v in venues)
    players=[j for j in all_jobs if j['event_key'].startswith('booking:')]
    assert len(players)==5 and {p['recipient'] for p in players}=={'p0@example.invalid','p1@example.invalid'}
    assert len(settings.venue_notices(owner))==3
    with client(world) as player,client(world) as owner_client:
        login(player,'p0@example.invalid');login(owner_client,'owner@example.invalid')
        own=player.get('/api/u/notices').json()['test_mail']
        assert len(own)==4 and all('Venue notice' not in j['subject'] for j in own)
        api_venues=owner_client.get(VENUE_NOTICES).json()['notices']
        assert [v['recipient'] for v in api_venues]==['venue-b@example.invalid','venue-a@example.invalid','venue-a@example.invalid']


def test_booking_and_venue_notice_roll_back_together_and_conflicts_do_not_duplicate(world,monkeypatch):
    no_network(monkeypatch)
    svc=world.state.unified;settings=world.state.test_integration_settings;owner=world.state.owner;db=world.state.engine.db
    p0,p1=fixture_players(world,2)
    settings.replace_booking_recipient(owner,0,'venue@example.invalid')
    # Force an error AFTER the booking insert fired both notification triggers.
    with db.tx() as c:
        c.execute("""CREATE TRIGGER test_forced_rollback BEFORE INSERT ON unified_booking_players
                     BEGIN SELECT RAISE(ABORT,'forced fixture rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError,match='forced fixture rollback'):
        svc.book_court(p0,DAY,10,1,1,[p0])
    with db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM unified_bookings').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM unified_booking_players').fetchone()[0]==0
    assert jobs(world)==[]
    with db.tx() as c:c.execute('DROP TRIGGER test_forced_rollback')
    def attempt(p):
        try:return svc.book_court(p,DAY,10,1,1,[p])
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(attempt,(p0,p1)))
    assert sum(x is not None for x in outcomes)==1
    assert len(jobs(world))==2 and len(venue_jobs(world))==1
    successful=next(x for x in outcomes if x is not None)
    assert venue_jobs(world)[0]['event_key']==f"venue_booking:{successful['booking_id']}"
    install_test_schema(db)
    assert len(jobs(world))==2 and len(venue_jobs(world))==1


def test_fake_payment_pending_failure_and_idempotent_success(world,tmp_path,monkeypatch):
    no_network(monkeypatch)
    owner=world.state.owner;db=world.state.engine.db
    settings=world.state.test_integration_settings
    settings.replace_booking_recipient(owner,0,'venue@example.invalid')
    player=fixture_players(world,1)[0]
    gateway=FictionalGateway(tmp_path/'separate-fictional-gateway.sqlite3',world.state.clock)
    lab=FictionalPaymentLab(world.state.unified,gateway)
    pending=lab.court(player,'test_pending_booking_01',DAY,10,1,1,[player])
    event=gateway.event(pending['payment_id'],'fevt_pending_booking_01')
    assert lab.callback(*event)['state']=='awaiting' and jobs(world)==[]
    gateway.set_fixture(pending['payment_id'],status='failed')
    failed=gateway.event(pending['payment_id'],'fevt_failed_booking_01')
    assert lab.callback(*failed)['state']=='failed' and jobs(world)==[]
    successful=lab.court(player,'test_success_booking_01',DAY,11,1,1,[player])
    gateway.set_fixture(successful['payment_id'],status='succeeded')
    event=gateway.event(successful['payment_id'],'fevt_success_booking_01')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:lab.callback(*event),range(4)))
    assert {r['state'] for r in results}=={'confirmed'}
    assert len({r['booking_id'] for r in results})==1
    assert lab.callback(*event)['booking_id']==results[0]['booking_id']
    assert len(jobs(world))==2 and len(venue_jobs(world))==1
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM unified_test_mail_jobs WHERE state='queued'").fetchone()[0]==2


def test_competing_venue_changes_share_revision_and_only_one_is_audited(world):
    settings=world.state.test_integration_settings;owner=world.state.owner
    def change(email):
        try:return settings.replace_booking_recipient(owner,0,email)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(change,('first@example.invalid','second@example.invalid')))
    assert sum(r is not None for r in results)==1
    current=settings.view(owner)
    assert current['revision']==1 and current['booking_recipient_email'] in ('first@example.invalid','second@example.invalid')
    with world.state.engine.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM auth_audit WHERE event='unified.test_booking_recipient_changed'").fetchone()[0]==1
