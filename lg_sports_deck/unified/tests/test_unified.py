from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date, datetime
import os
import time
import pytest
from fastapi.testclient import TestClient

from lg_sports_deck.core.domain import Conflict, PermissionDenied
from lg_sports_deck.unified.app import create_app
from lg_sports_deck.unified.domain import MANILA

ORIGIN='https://testserver'
PW='Fictional account password 47'

class Clock:
    def __init__(self):self.t=datetime(2027,6,2,10,0,tzinfo=MANILA).timestamp()
    def __call__(self):return self.t

@pytest.fixture
def world(tmp_path):
    clock=Clock();app=create_app(tmp_path/'unified.sqlite3',tmp_path/'media',{ORIGIN},clock=clock,
                                 ffmpeg_path=os.environ.get('FFMPEG_TEST_BIN'))
    owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Fictional Owner',PW)
    app.state.unified.set_limits(owner,4,12)
    app.state.owner=owner;app.state.clock=clock
    yield app
    app.state.recorder.close()


def client(app):return TestClient(app,base_url=ORIGIN)
def anon(c):
    r=c.get('/api/auth/bootstrap');assert r.status_code==200
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf']}
def login(c,email,pw=PW):
    r=c.post('/api/auth/login',headers=anon(c),json={'email':email,'password':pw});assert r.status_code==200,r.text
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf']}
def register(c,email,name):
    r=c.post('/api/auth/register',headers=anon(c),json={'email':email,'name':name,'password':PW});assert r.status_code==200,r.text
    h=login(c,email);me=c.get('/api/auth/me').json();return h,me['id']
def notice(c,h):assert c.post('/api/u/notice/accept',headers=h,json={}).status_code==200

def create_staff(owner,h,email,role):
    r=owner.post('/api/staff',headers=h,json={'email':email,'name':'Fictional '+role,'password':PW,'role':role})
    assert r.status_code==200,r.text;return r.json()['test_staff_id']

def grants(owner,h,uid,permissions):
    return owner.post(f'/api/staff/{uid}/permissions',headers=h,json={'permissions':permissions})

def create_session(owner,h,*,start=8,duration=3,day='2027-06-02'):
    r=owner.post('/api/u/sessions',headers=h,json={'day':day,'start_hour':start,'duration':duration,
                  'label':'Fictional Open Play','fictional_venue_token':'fixture-token-47'})
    assert r.status_code==200,r.text;return r.json()['session_id']

def fixture_players(app,n):
    a=app.state.accounts
    ids=[]
    for i in range(n):
        p=a.register_test_player(f'p{i}@example.invalid',f'Player {i}',PW);a.accept_test_notice(p);ids.append(p)
    return ids


def test_offline_bootstrap_produces_a_usable_unified_test_db(tmp_path,monkeypatch):
    import sys
    from lg_sports_deck.accounts import bootstrap
    path=tmp_path/'new-test.sqlite3'
    monkeypatch.setattr(sys,'argv',['bootstrap','--db',str(path),'--email','owner@example.invalid'])
    monkeypatch.setattr(bootstrap.getpass,'getpass',lambda prompt:PW)
    bootstrap.main()
    app=create_app(path,tmp_path/'media',{ORIGIN})
    with TestClient(app,base_url=ORIGIN) as c:
        login(c,'owner@example.invalid')
        assert c.get('/api/auth/me').json()['role']=='superuser'
        assert c.get('/api/u/catalog').json()['sessions']==[]
    with pytest.raises(Conflict):bootstrap.main()
    app.state.recorder.close()


def test_old_demo_and_checkout_routes_are_not_mounted_permissions_default_deny(world):
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));admin=stack.enter_context(client(world));manager=stack.enter_context(client(world))
        assert owner.get('/api/u/catalog').status_code==401
        oh=login(owner,'owner@example.invalid');aid=create_staff(owner,oh,'admin@example.invalid','admin');mid=create_staff(owner,oh,'manager@example.invalid','manager')
        ah=login(admin,'admin@example.invalid');mh=login(manager,'manager@example.invalid')
        assert owner.post('/api/demo/role',headers=oh,json={'role':'staff'}).status_code==404
        assert owner.post('/api/test/checkout',headers=oh,json={'session_id':1,'player_ids':[1]}).status_code==404
        assert owner.post('/api/ops/test-rate',headers=oh,json={'amount':1}).status_code==404
        sid=create_session(owner,oh)
        assert admin.get('/api/u/catalog').json()['sessions']==[]
        assert admin.get('/api/u/ops/bookings').status_code==403
        assert admin.get('/api/u/ops/sales').status_code==403
        assert admin.get('/api/u/matches').status_code==403
        assert admin.post('/api/u/sessions',headers=ah,json={'day':'2027-06-02','start_hour':12,'duration':3,'label':'Test','fictional_venue_token':'fixture-token'}).status_code==403
        assert admin.post('/api/u/settings/rates',headers=ah,json={'open_rate':1,'court_rate':1,'paddle_rate':1}).status_code==403
        assert admin.post('/api/u/ops/no-show',headers=ah,json={'session_id':sid,'player_id':1}).status_code==403
        assert admin.post(f'/api/staff/{aid}/permissions',headers=ah,json={'permissions':['queue.manage']}).status_code==403
        assert grants(owner,oh,aid,['recordings.view']).status_code==422
        assert grants(owner,oh,aid,['session.view','bookings.view','sales.view']).status_code==200
        assert len(admin.get('/api/u/catalog').json()['sessions'])==1
        assert admin.get('/api/u/ops/bookings').status_code==200 and admin.get('/api/u/ops/sales').status_code==200
        assert grants(owner,oh,aid,[]).status_code==200
        assert admin.get('/api/u/ops/bookings').status_code==403 and admin.get('/api/u/ops/sales').status_code==403
        assert owner.post('/api/u/settings/limits',headers={**oh,'Origin':'https://attacker.invalid'},json={'open_limit':4,'court_limit':12}).status_code==403
        assert owner.post('/api/u/settings/limits',headers={'Origin':ORIGIN},json={'open_limit':4,'court_limit':12}).status_code==403
        assert manager.get('/api/auth/me',headers={'X-Role':'superuser','X-User-ID':str(world.state.owner)}).json()['role']=='manager'


def test_integrated_booking_boundaries_group_notices_and_sales(world):
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        people=[]
        for i in range(5):
            c=stack.enter_context(client(world));h,uid=register(c,f'book{i}@example.invalid',f'Book {i}');people.append((c,h,uid))
        for c,h,_ in people[:3]:notice(c,h)
        ids=[p[2] for p in people[:4]]
        assert people[0][0].post('/api/u/notice/accept',headers=people[0][1],json={'user_id':ids[3]}).status_code==422
        group={'session_id':sid,'player_ids':ids}
        assert people[0][0].post('/api/u/book/openplay',headers=people[0][1],json=group).status_code==409
        with world.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM session_registrations').fetchone()[0]==0
        notice(people[3][0],people[3][1]);r=people[0][0].post('/api/u/book/openplay',headers=people[0][1],json=group)
        assert r.status_code==200 and r.json()['amount']==800
        payload={'day':'2027-06-02','start_hour':10,'duration':1,'court':1,'player_ids':ids}
        assert people[0][0].post('/api/u/book/court',headers=people[0][1],json=payload).status_code==409
        assert people[0][0].post('/api/u/book/court',headers=people[0][1],json={**payload,'court':2}).status_code==409
        payload.update(start_hour=11,duration=2)
        r=people[0][0].post('/api/u/book/court',headers=people[0][1],json=payload);assert r.status_code==200 and r.json()['amount']==500
        assert people[0][0].post('/api/u/book/court',headers=people[0][1],json=payload).status_code==409
        assert people[0][0].post('/api/u/book/court',headers=people[0][1],json={**payload,'court':2}).status_code==200
        assert people[0][0].post('/api/u/book/court',headers=people[0][1],json={**payload,'start_hour':23,'duration':2}).status_code==422
        assert owner.post('/api/u/sessions',headers=oh,json={'day':'2027-06-02','start_hour':12,'duration':3,'label':'Collision','fictional_venue_token':'fixture-token'}).status_code==409
        assert len(people[1][0].get('/api/u/my-bookings').json()['bookings'])==3
        assert people[4][0].get('/api/u/my-bookings').json()['bookings']==[]
        daily=owner.get('/api/u/ops/sales?period=daily').json();monthly=owner.get('/api/u/ops/sales?period=monthly').json()
        assert daily['total']==1800 and monthly['total']==1800 and daily['count']==3
        assert {x['kind'] for x in daily['breakdown']}=={'court','openplay'}
        assert {x['payment_method'] for x in daily['breakdown']}=={'simulated_gcash'}


def test_simulated_booking_concurrency_serializes_capacity_and_court(world):
    svc=world.state.unified;owner=world.state.owner;sid=svc.create_session(owner,date(2027,6,2),8,3,'Test','fictional-token')
    p=fixture_players(world,7);svc.book_openplay(p[0],sid,p[:3])
    def last(player):
        try:svc.book_openplay(player,sid,[player]);return 'ok'
        except Conflict:return 'full'
    with ThreadPoolExecutor(max_workers=2) as pool:res=list(pool.map(last,p[3:5]))
    assert sorted(res)==['full','ok']
    def court(player):
        try:svc.book_court(player,date(2027,6,2),12,1,1,[player]);return 'ok'
        except Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:res=list(pool.map(court,p[5:7]))
    assert sorted(res)==['conflict','ok']
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(sid,)).fetchone()[0]==4
        assert c.execute("SELECT count(*) FROM unified_bookings WHERE kind='court'").fetchone()[0]==1


def test_no_show_waitlist_unsent_notices_and_first_payment_wins(world):
    svc=world.state.unified;owner=world.state.owner;sid=svc.create_session(owner,date(2027,6,2),8,3,'Test','fictional-token')
    p=fixture_players(world,7);svc.book_openplay(p[0],sid,p[:4])
    svc.join_waitlist(p[4],sid);svc.join_waitlist(p[5],sid)
    assert svc.mark_no_show(owner,sid,p[0])==2
    assert len(svc.notices(p[4])['waitlist'])==1 and len(svc.notices(p[5])['waitlist'])==1
    with pytest.raises(Conflict):svc.book_openplay(p[6],sid,[p[6]])
    def claim(player):
        try:svc.book_openplay(player,sid,[player]);return 'paid'
        except Conflict:return 'full'
    with ThreadPoolExecutor(max_workers=2) as pool:res=list(pool.map(claim,p[4:6]))
    assert sorted(res)==['full','paid']
    with pytest.raises(Conflict):svc.mark_no_show(owner,sid,p[0])
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(sid,)).fetchone()[0]==4
        assert c.execute('SELECT attendance FROM unified_booking_players WHERE user_id=?',(p[0],)).fetchone()[0]=='no_show'
        assert c.execute('SELECT sum(amount) FROM unified_bookings').fetchone()[0]==1000
    assert svc.sales(owner,'daily')['total']==1000


def test_checked_in_player_cannot_be_marked_no_show(world):
    svc=world.state.unified;owner=world.state.owner;sid=svc.create_session(owner,date(2027,6,2),8,3,'Test','fictional-token')
    p=fixture_players(world,4);svc.book_openplay(p[0],sid,p)
    world.state.engine.check_in(sid,p[0],'fictional-token')
    with pytest.raises(Conflict):svc.mark_no_show(owner,sid,p[0])


def test_authenticated_openplay_match_replay_privacy_and_confirmed_rankings(world):
    if not os.environ.get('FFMPEG_TEST_BIN'):pytest.skip('Synthetic FFmpeg not enabled')
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        aid=create_staff(owner,oh,'admin@example.invalid','admin');mid=create_staff(owner,oh,'manager@example.invalid','manager')
        admin=stack.enter_context(client(world));ah=login(admin,'admin@example.invalid');manager=stack.enter_context(client(world));mh=login(manager,'manager@example.invalid')
        all_grants=owner.get('/api/staff').json()['grantable']
        assert 'payments.manual_verify' in all_grants  # even this grant must never allow staff replay access
        assert grants(owner,oh,aid,all_grants).status_code==200 and grants(owner,oh,mid,all_grants).status_code==200
        people=[]
        for i in range(5):
            c=stack.enter_context(client(world));h,uid=register(c,f'game{i}@example.invalid',f'Game {i}');notice(c,h);people.append((c,h,uid))
        ids=[x[2] for x in people[:4]]
        assert people[0][0].post('/api/u/book/openplay',headers=people[0][1],json={'session_id':sid,'player_ids':ids}).status_code==200
        assert people[4][0].post('/api/u/check-in',headers={**people[4][1],'X-User-Id':str(ids[0])},json={'session_id':sid,'fictional_venue_token':'fixture-token-47'}).status_code==403
        for c,h,_ in people[:4]:assert c.post('/api/u/check-in',headers=h,json={'session_id':sid,'fictional_venue_token':'fixture-token-47'}).status_code==200
        assert [x['user_id'] for x in admin.get(f'/api/u/queue/{sid}').json()['queue']]==ids
        chosen={'session_id':sid,'court':1,'team_a':[ids[0],ids[2]],'team_b':[ids[1],ids[3]]}
        r=admin.post('/api/u/queue/assign-selected',headers=ah,json=chosen);assert r.status_code==200;match=r.json()['match_id'];base=f'/api/u/matches/{match}'
        assert admin.post(base+'/recording/start',headers=ah).status_code==403
        assert manager.post(base+'/recording/start',headers=mh).status_code==403
        assert people[4][0].post(base+'/recording/start',headers=people[4][1]).status_code==403
        assert people[0][0].post(base+'/recording/start',headers=people[0][1]).status_code==200
        time.sleep(.5)
        assert people[1][0].post(base+'/recording/stop',headers=people[1][1]).json()['state']=='ready'
        assert people[0][0].get(base+'/replay').status_code==200 and owner.get(base+'/replay').status_code==200
        assert admin.get(base+'/replay').status_code==403 and manager.get(base+'/replay').status_code==403 and people[4][0].get(base+'/replay').status_code==403
        matches=admin.get('/api/u/matches').json()['matches'];assert matches[0]['recording_state'] is None and not matches[0]['can_watch']
        notices=people[0][0].get('/api/u/notices').json()['matches'];assert len(notices)==1 and notices[0]['kind']=='replay' and 'missing' in notices[0]['body']
        assert admin.post(base+'/end',headers=ah).status_code==200
        assert len(admin.get(f'/api/u/queue/{sid}').json()['queue'])==4
        assert admin.post(base+'/score',headers=ah,json={'score_a':11,'score_b':9}).status_code==200
        assert admin.post(base+'/confirm',headers=ah).status_code==403
        assert people[0][0].post(base+'/confirm',headers=people[0][1]).json()['confirmed'] is False
        assert people[0][0].get('/api/u/matches').json()['matches'][0]['can_confirm'] is False
        assert people[1][0].get('/api/u/matches').json()['matches'][0]['can_confirm'] is True
        assert people[0][0].get('/api/u/ranking').json()['ranking']==[]
        assert people[1][0].post(base+'/confirm',headers=people[1][1]).json()['confirmed'] is True
        ranking=people[0][0].get('/api/u/ranking').json()['ranking'];assert len(ranking)==4 and sum(r['wins'] for r in ranking)==2
        assert people[0][0].get('/api/u/matches').json()['matches'][0]['can_score'] is False
        assert {n['kind'] for n in people[0][0].get('/api/u/notices').json()['matches']}=={'replay','result'}


def test_authenticated_rental_uses_booked_accounts_and_is_unranked(world):
    if not os.environ.get('FFMPEG_TEST_BIN'):pytest.skip('Synthetic FFmpeg not enabled')
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid')
        aid=create_staff(owner,oh,'admin@example.invalid','admin');assert grants(owner,oh,aid,owner.get('/api/staff').json()['grantable']).status_code==200
        admin=stack.enter_context(client(world));ah=login(admin,'admin@example.invalid')
        people=[]
        for i in range(5):
            c=stack.enter_context(client(world));h,uid=register(c,f'rental{i}@example.invalid',f'Rental {i}');notice(c,h);people.append((c,h,uid))
        ids=[x[2] for x in people[:4]]
        r=people[0][0].post('/api/u/book/court',headers=people[0][1],json={'day':'2027-06-02','start_hour':12,'duration':2,'court':2,'player_ids':ids})
        assert r.status_code==200 and r.json()['amount']==500;booking=r.json()['booking_id']
        assert people[4][0].post('/api/u/rental-match',headers=people[4][1],json={'booking_id':booking,'player_ids':ids}).status_code==422
        assert people[0][0].post('/api/u/rental-match',headers=people[0][1],json={'booking_id':booking,'player_ids':[ids[0],ids[1],ids[2],people[4][2]]}).status_code==403
        r=people[0][0].post('/api/u/rental-match',headers=people[0][1],json={'booking_id':booking,'player_ids':ids})
        assert r.status_code==200;match=r.json()['match_id'];base=f'/api/u/matches/{match}'
        assert people[3][0].post(base+'/recording/start',headers=people[3][1]).status_code==200
        time.sleep(.5)
        assert people[2][0].post(base+'/recording/stop',headers=people[2][1]).json()['state']=='ready'
        assert people[0][0].get(base+'/replay').status_code==200 and owner.get(base+'/replay').status_code==200
        assert admin.get(base+'/replay').status_code==403 and people[4][0].get(base+'/replay').status_code==403
        assert admin.get('/api/u/matches').json()['matches']==[]
        assert people[0][0].get('/api/u/ranking').json()['ranking']==[]
        assert people[0][0].get('/api/u/notices').json()['matches']==[]


def test_concurrent_session_creation_and_rental_cannot_overlap(world):
    svc=world.state.unified;owner=world.state.owner;p=fixture_players(world,1)[0]
    def session():
        try:svc.create_session(owner,date(2027,6,2),12,3,'Concurrent session','fictional-token');return 'ok'
        except Conflict:return 'conflict'
    def court():
        try:svc.book_court(p,date(2027,6,2),12,1,1,[p]);return 'ok'
        except Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(session),pool.submit(court)];results=[f.result() for f in futures]
    assert sorted(results)==['conflict','ok']
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_sessions').fetchone()[0]+c.execute("SELECT count(*) FROM unified_bookings WHERE kind='court'").fetchone()[0]==1


def test_expired_test_slots_and_past_rental_are_rejected(world):
    svc=world.state.unified;owner=world.state.owner;sid=svc.create_session(owner,date(2027,6,2),8,3,'Test','fictional-token')
    p=fixture_players(world,2)
    with pytest.raises(Conflict):svc.book_court(p[0],date(2027,6,2),9,1,1,[p[0]])
    world.state.clock.t=datetime(2027,6,2,11,0,tzinfo=MANILA).timestamp()
    with pytest.raises(Conflict):svc.book_openplay(p[0],sid,[p[0]])
    with pytest.raises(Conflict):svc.join_waitlist(p[1],sid)
    assert svc.catalog(p[0])['sessions'][0]['ended'] is True


def test_manual_queue_order_permissions_stale_snapshot_append_and_assignment(world):
    svc=world.state.unified;owner_id=world.state.owner;svc.set_limits(owner_id,8,12)
    sid=svc.create_session(owner_id,date(2027,6,2),8,3,'Fictional queue','fictional-token')
    players=fixture_players(world,6);svc.book_openplay(players[0],sid,players)
    for player in players[:5]:world.state.engine.check_in(sid,player,'fictional-token')
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid')
        aid=create_staff(owner,oh,'queue-admin@example.invalid','admin')
        staff=stack.enter_context(client(world));sh=login(staff,'queue-admin@example.invalid')
        player=stack.enter_context(client(world));ph=login(player,'p0@example.invalid')
        snap=owner.get(f'/api/u/queue/{sid}').json();ids=[r['id'] for r in snap['queue']]
        payload={'session_id':sid,'expected_ids':ids,'ordered_ids':ids[::-1],'expected_revision':snap['revision']}
        assert staff.post('/api/u/queue/reorder',headers=sh,json=payload).status_code==403
        assert player.post('/api/u/queue/reorder',headers=ph,json=payload).status_code==403
        assert grants(owner,oh,aid,['session.view','queue.view','queue.manage']).status_code==200
        assert staff.post('/api/u/queue/reorder',headers={**sh,'Origin':'https://attacker.invalid'},json=payload).status_code==403
        invalid={**payload,'ordered_ids':[ids[0]]*len(ids)}
        assert staff.post('/api/u/queue/reorder',headers=sh,json=invalid).status_code==422
        assert [r['id'] for r in owner.get(f'/api/u/queue/{sid}').json()['queue']]==ids
        assert staff.post('/api/u/queue/reorder',headers=sh,json=payload).status_code==200
        assert staff.post('/api/u/queue/reorder',headers=sh,json=payload).status_code==409
        # New check-ins join behind manually rearranged entries, without losing order.
        world.state.engine.check_in(sid,players[5],'fictional-token')
        ordered=owner.get(f'/api/u/queue/{sid}').json()['queue']
        assert [r['user_id'] for r in ordered]==players[:5][::-1]+[players[5]]
        stale_ids=[r['id'] for r in ordered]; stale_revision=svc.queue_revision(sid,ordered)
        # A score/classification change must invalidate the view even if IDs do not change.
        with world.state.engine.db.tx() as c:c.execute("UPDATE queue_entries SET classification='pending' WHERE id=?",(stale_ids[0],))
        stale={'session_id':sid,'expected_ids':stale_ids,'ordered_ids':stale_ids[::-1],'expected_revision':stale_revision}
        assert staff.post('/api/u/queue/reorder',headers=sh,json=stale).status_code==409
        match=staff.post('/api/u/queue/assign',headers=sh,json={'session_id':sid,'court':1})
        assert match.status_code==200
        chosen={p['user_id'] for p in world.state.engine.match(match.json()['match_id'])['players']}
        assert chosen==set(players[:5][::-1][:4])
        with world.state.engine.db.read() as c:
            assert c.execute("SELECT count(*) FROM auth_audit WHERE event='unified.queue_reordered'").fetchone()[0]==1


def test_reordered_priority_is_used_for_winner_and_loser_groups(world):
    svc=world.state.unified;owner=world.state.owner;svc.set_limits(owner,8,12)
    sid=svc.create_session(owner,date(2027,6,2),8,3,'Fictional groups','fictional-token')
    players=fixture_players(world,8);svc.book_openplay(players[0],sid,players)
    for p in players:world.state.engine.check_in(sid,p,'fictional-token')
    rows=svc.queue(owner,sid);ids=[r['id'] for r in rows]
    with world.state.engine.db.tx() as c:
        c.executemany('UPDATE queue_entries SET classification=? WHERE id=?',
                      [('loser' if i<4 else 'winner',qid) for i,qid in enumerate(ids)])
    rows=svc.queue(owner,sid);revision=svc.queue_revision(sid,rows)
    svc.reorder_queue(owner,sid,ids,ids[4:]+ids[:4],revision)
    mid=world.state.engine.assign_next(owner,sid,1)
    assert {p['user_id'] for p in world.state.engine.match(mid)['players']}==set(players[4:])


def test_concurrent_queue_reorders_do_not_overwrite_one_another(world):
    svc=world.state.unified;owner=world.state.owner;svc.set_limits(owner,8,12)
    sid=svc.create_session(owner,date(2027,6,2),8,3,'Fictional race','fictional-token')
    players=fixture_players(world,5);svc.book_openplay(players[0],sid,players)
    for p in players:world.state.engine.check_in(sid,p,'fictional-token')
    rows=svc.queue(owner,sid);ids=[r['id'] for r in rows];revision=svc.queue_revision(sid,rows)
    def reorder(order):
        try:svc.reorder_queue(owner,sid,ids,order,revision);return 'ok'
        except Conflict:return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(reorder,[ids[::-1],ids[1:]+ids[:1]]))
    assert sorted(result)==['ok','stale']
    assert [r['id'] for r in svc.queue(owner,sid)] in (ids[::-1],ids[1:]+ids[:1])


def test_notification_jobs_are_atomic_private_and_follow_match_events(world,tmp_path):
    from lg_sports_deck.core.recording import RecordingCoordinator
    from lg_sports_deck.unified.notifications import install_test_schema, render_test_mime
    svc=world.state.unified;engine=world.state.engine;owner=world.state.owner
    sid=svc.create_session(owner,date(2027,6,2),8,3,'Fictional session','fictional-token')
    players=fixture_players(world,6);svc.book_openplay(players[0],sid,players[:4])
    assert [j['kind'] for j in svc.notices(players[0])['test_mail']]==['booking_confirmation']
    assert svc.notices(players[1])['test_mail']==[]  # booking recipient policy remains provisional
    svc.join_waitlist(players[4],sid);svc.join_waitlist(players[5],sid)
    assert svc.mark_no_show(owner,sid,players[0])==2
    svc.book_openplay(players[4],sid,[players[4]])
    assert {j['kind'] for j in svc.notices(players[4])['test_mail']}=={'waitlist_open','booking_confirmation'}
    assert {j['kind'] for j in svc.notices(players[5])['test_mail']}=={'waitlist_open'}
    current=[players[1],players[2],players[3],players[4]]
    for p in current:engine.check_in(sid,p,'fictional-token')
    mid=engine.assign_next(owner,sid,1)
    class FictionalBackend:
        def start(self,court_id,match_id,destination):return destination
        def stop(self,handle):handle.write_bytes(b'fictional test pattern');return handle
        def abort(self,handle):pass
    recorder=RecordingCoordinator(engine,FictionalBackend(),tmp_path/'fake-notification-media')
    try:
        recorder.start(current[0],mid);assert recorder.stop(current[0],mid)['state']=='ready'
        for p in current:
            jobs=svc.notices(p)['test_mail']
            assert sum(j['kind']=='replay' and j['match_id']==mid for j in jobs)==1
            assert not any(j['kind']=='result' for j in jobs)
        assert all(j['kind']!='replay' for j in svc.notices(players[5])['test_mail'])
        with engine.db.read() as c:
            replay_job=c.execute("SELECT id FROM unified_test_mail_jobs WHERE user_id=? AND kind='replay'",(current[0],)).fetchone()[0]
        message=render_test_mime(engine.db,replay_job,'https://preview.example.invalid','sender@example.invalid')
        assert message['To'].endswith('@example.invalid') and message.get('Cc') is None and message.get('Bcc') is None
        assert f'https://preview.example.invalid/#match-{mid}' in message.get_content()
        assert 'participant authorization' in message.get_content() and 'Automatic real-media expiry is not implemented' in message.get_content()
        assert message['Message-ID']==render_test_mime(engine.db,replay_job,'https://preview.example.invalid','sender@example.invalid')['Message-ID']
        for origin in ('https://real.example.com','http://preview.example.invalid','https://user:pw@preview.example.invalid','https://preview.example.invalid/path'):
            with pytest.raises(ValueError):render_test_mime(engine.db,replay_job,origin,'sender@example.invalid')
        with pytest.raises(ValueError):render_test_mime(engine.db,replay_job,'https://preview.example.invalid','real@example.com')
        engine.end_match(current[0],mid);engine.submit_score(current[0],mid,11,9)
        teams=engine.match(mid)['players'];a=next(x['user_id'] for x in teams if x['team']=='A');b=next(x['user_id'] for x in teams if x['team']=='B')
        assert engine.confirm_score(a,mid) is False
        assert all(not any(j['kind']=='result' for j in svc.notices(p)['test_mail']) for p in current)
        assert engine.confirm_score(b,mid) is True and engine.confirm_score(b,mid) is True
        assert all(sum(j['kind']=='result' for j in svc.notices(p)['test_mail'])==1 for p in current)
        with engine.db.read() as c:before=c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]
        install_test_schema(engine.db);install_test_schema(engine.db) # idempotent backfill
        with engine.db.read() as c:assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==before
        with pytest.raises(RuntimeError):
            with engine.db.tx() as c:
                c.execute("INSERT INTO unified_notifications(user_id,session_id,kind,message,created_at) VALUES(?,?,'place_opened','fictional rollback',0)",(players[5],sid))
                assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==before+1
                raise RuntimeError('roll back fictional event and its job')
        with engine.db.read() as c:assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==before
    finally:recorder.close()


def test_simulated_mail_retry_unknown_and_attempt_limits_never_send_email(world,monkeypatch):
    import socket
    from lg_sports_deck.unified.notifications import SimulatedMailWorker
    svc=world.state.unified;p=fixture_players(world,1)[0]
    svc.book_court(p,date(2027,6,2),12,1,1,[p])
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network forbidden')))
    worker=SimulatedMailWorker(world.state.engine.db,world.state.clock,max_attempts=2)
    first=worker.claim();assert first and first.attempt==1 and first.recipient.endswith('.invalid')
    assert worker.finish(first,'transient')=='retry_wait' and worker.claim() is None
    world.state.clock.t+=60
    second=worker.claim();assert second and second.job_id==first.job_id and second.attempt==2
    assert worker.finish(second,'unknown')=='uncertain' and worker.finish(second,'success')=='stale'
    world.state.clock.t+=10000;assert worker.claim() is None
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT state FROM unified_test_mail_jobs').fetchone()[0]=='uncertain'
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_simulated_mail_expired_claim_fails_closed_and_disabled_user_is_cancelled(world):
    from lg_sports_deck.unified.notifications import SimulatedMailWorker
    svc=world.state.unified;people=fixture_players(world,2)
    svc.book_court(people[0],date(2027,6,2),12,1,1,[people[0]])
    svc.book_court(people[1],date(2027,6,2),13,1,1,[people[1]])
    worker=SimulatedMailWorker(world.state.engine.db,world.state.clock,lease_seconds=10)
    first=worker.claim();world.state.clock.t+=11
    second=worker.claim();assert first and second and first.job_id!=second.job_id
    assert worker.finish(first,'success')=='stale'
    with world.state.engine.db.tx() as c:c.execute('UPDATE users SET enabled=0 WHERE id=?',(people[1],))
    assert worker.finish(second,'success')=='cancelled'
    with world.state.engine.db.read() as c:
        assert [r['state'] for r in c.execute('SELECT state FROM unified_test_mail_jobs ORDER BY id')]==['uncertain','cancelled']
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_simulated_mail_concurrent_claims_and_receipts_are_unique(world,monkeypatch):
    import socket
    from lg_sports_deck.unified.notifications import SimulatedMailWorker
    svc=world.state.unified;p=fixture_players(world,1)[0]
    for hour in range(12,18):svc.book_court(p,date(2027,6,2),hour,1,1,[p])
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network forbidden')))
    worker=SimulatedMailWorker(world.state.engine.db,world.state.clock)
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:worker.run_one(),range(10)))
    completed=[r for r in results if r is not None]
    assert len(completed)==6 and len({r[0] for r in completed})==6 and all(r[1]=='simulated' for r in completed)
    with world.state.engine.db.read() as c:
        assert c.execute("SELECT count(*) FROM unified_test_mail_jobs WHERE state='simulated'").fetchone()[0]==6
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==6


def test_simulated_mail_rejects_nonfictional_recipients_and_exhausts_retries(world):
    from lg_sports_deck.unified.notifications import SimulatedMailWorker
    svc=world.state.unified;p=fixture_players(world,1)[0]
    svc.book_court(p,date(2027,6,2),12,1,1,[p]);svc.book_court(p,date(2027,6,2),13,1,1,[p])
    with world.state.engine.db.tx() as c:
        c.execute("UPDATE unified_test_mail_jobs SET recipient='real@example.com' WHERE id=(SELECT MIN(id) FROM unified_test_mail_jobs)")
    worker=SimulatedMailWorker(world.state.engine.db,world.state.clock,max_attempts=2)
    first=worker.claim();assert first and first.recipient.endswith('.invalid')
    assert worker.finish(first,'transient')=='retry_wait';world.state.clock.t+=60
    second=worker.claim();assert second and second.attempt==2
    assert worker.finish(second,'transient')=='failed' and worker.claim() is None
    with world.state.engine.db.read() as c:
        assert [tuple(r) for r in c.execute('SELECT state,failure_code FROM unified_test_mail_jobs ORDER BY id')]==[
            ('failed','invalid_test_recipient'),('failed','test_attempt_limit')]
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0


def test_additive_queue_upgrade_preserves_existing_fifo_entries(world):
    from lg_sports_deck.core.domain import Database, Engine
    svc=world.state.unified;owner=world.state.owner
    sid=svc.create_session(owner,date(2027,6,2),8,3,'Old fictional queue','fictional-token')
    players=fixture_players(world,4);svc.book_openplay(players[0],sid,players)
    for player in players:world.state.engine.check_in(sid,player,'fictional-token')
    before=world.state.engine.queue(sid)
    # Simulate a pre-upgrade test database using this disposable fixture only.
    with world.state.engine.db.tx() as c:c.execute('DROP TABLE queue_positions')
    upgraded=Database(world.state.engine.db.path)
    assert Engine(upgraded).queue(sid)==before
    with upgraded.read() as c:assert c.execute("SELECT count(*) FROM queue_positions").fetchone()[0]==0


def test_notification_upgrade_backfills_fictional_events_without_sending(world):
    from lg_sports_deck.unified.notifications import install_test_schema
    db=world.state.engine.db;svc=world.state.unified;owner=world.state.owner
    # Simulate the previous schema in this disposable test DB, not a venue DB.
    with db.tx() as c:
        for name in ('unified_test_mail_booking','unified_test_mail_venue_booking','unified_test_mail_waitlist','unified_test_mail_match'):
            c.execute('DROP TRIGGER '+name)
        c.execute('DROP TABLE unified_test_mail_receipts');c.execute('DROP TABLE unified_test_mail_jobs')
    sid=svc.create_session(owner,date(2027,6,2),8,3,'Old fictional session','fictional-token')
    players=fixture_players(world,5);svc.book_openplay(players[0],sid,players[:4]);svc.join_waitlist(players[4],sid)
    assert svc.mark_no_show(owner,sid,players[0])==1
    for _ in range(2):install_test_schema(db)
    with db.read() as c:
        assert [r['kind'] for r in c.execute('SELECT kind FROM unified_test_mail_jobs ORDER BY id')]==['booking_confirmation','waitlist_open']
        assert c.execute('SELECT count(*) FROM unified_test_mail_receipts').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_notifications').fetchone()[0]==1


def _decode_fictional_qr(data_url):
    """Decode the generated QR with the same vendored jsQR used by the UI."""
    import base64, io, subprocess
    from pathlib import Path
    from PIL import Image
    prefix='data:image/png;base64,'
    assert data_url.startswith(prefix)
    image=Image.open(io.BytesIO(base64.b64decode(data_url[len(prefix):],validate=True))).convert('RGBA')
    decoder=Path(__file__).resolve().parents[1]/'static/vendor/jsQR-1.4.0.js'
    script='''const decode=require(process.argv[1]); const data=[];
      process.stdin.on('data',b=>data.push(b)); process.stdin.on('end',()=>{
      const buf=Buffer.concat(data); const bytes=new Uint8ClampedArray(buf);
      const result=decode(bytes,Number(process.argv[2]),Number(process.argv[3]));
      if(!result)process.exit(2); process.stdout.write(result.data);});'''
    result=subprocess.run(['node','-e',script,str(decoder),str(image.width),str(image.height)],
                          input=image.tobytes(),capture_output=True,check=True,timeout=15)
    parts=result.stdout.decode('ascii').split(':')
    assert len(parts)==3 and parts[0]=='LGSDTEST1' and parts[1].isdigit() and len(parts[2])==32
    return int(parts[1]),parts[2]


def test_fictional_rotating_qr_permissions_scan_and_idempotent_queue(world):
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        aid=create_staff(owner,oh,'qradmin@example.invalid','admin')
        admin=stack.enter_context(client(world));ah=login(admin,'qradmin@example.invalid')
        people=[]
        for i in range(3):
            c=stack.enter_context(client(world));h,uid=register(c,f'qr{i}@example.invalid',f'QR Player {i}');notice(c,h);people.append((c,h,uid))
        assert people[0][0].post('/api/u/book/openplay',headers=people[0][1],json={
            'session_id':sid,'player_ids':[people[0][2],people[1][2]]}).status_code==200
        req={'session_id':sid}
        assert owner.post('/api/u/test-qr/issue',json=req).status_code==403
        assert admin.post('/api/u/test-qr/issue',headers=ah,json=req).status_code==403
        assert people[0][0].post('/api/u/test-qr/issue',headers=people[0][1],json=req).status_code==403
        assert grants(owner,oh,aid,['session.view','attendance.qr.display']).status_code==200
        issued=admin.post('/api/u/test-qr/issue',headers=ah,json=req)
        assert issued.status_code==200 and issued.headers['cache-control']=='no-store'
        j=issued.json();qr_session,challenge=_decode_fictional_qr(j['qr_image'])
        assert qr_session==sid and j['lifetime_seconds']==180 and j['expires_at']==int(world.state.clock.t)+180
        with world.state.engine.db.read() as c:
            row=c.execute('SELECT * FROM unified_test_qr_challenges WHERE session_id=?',(sid,)).fetchone()
            assert challenge not in str(dict(row)) and len(row['challenge_hash'])==64
            assert all(challenge not in str(dict(x)) for x in c.execute('SELECT * FROM auth_audit'))
        payload={'session_id':sid,'challenge':challenge}
        assert people[2][0].post('/api/u/test-qr/check-in',headers=people[2][1],json=payload).status_code==403
        assert people[0][0].post('/api/u/test-qr/check-in',headers=people[0][1],json={**payload,'challenge':'X'*32}).status_code==403
        first=people[0][0].post('/api/u/test-qr/check-in',headers=people[0][1],json=payload)
        duplicate=people[0][0].post('/api/u/test-qr/check-in',headers=people[0][1],json=payload)
        assert first.status_code==duplicate.status_code==200
        assert first.headers['cache-control']=='no-store' and first.json()['created'] is True
        assert duplicate.json()['created'] is False and first.json()['checkin_id']==duplicate.json()['checkin_id']
        assert people[1][0].post('/api/u/test-qr/check-in',headers=people[1][1],json=payload).json()['created'] is True
        assert [p['user_id'] for p in owner.get(f'/api/u/queue/{sid}').json()['queue']]==[people[0][2],people[1][2]]
        assert grants(owner,oh,aid,[]).status_code==200
        assert admin.post('/api/u/test-qr/issue',headers=ah,json=req).status_code==403


def test_fictional_qr_rotation_expiry_no_show_and_concurrent_idempotency(world):
    service=world.state.unified;owner=world.state.owner
    sid=service.create_session(owner,date(2027,6,2),8,3,'QR test','fictional-token')
    players=fixture_players(world,4);service.book_openplay(players[0],sid,players)
    qr=world.state.test_qr
    first=qr.issue(owner,sid);first_sid,old=_decode_fictional_qr(first['qr_image'])
    assert first_sid==sid
    second=qr.issue(owner,sid);_,new=_decode_fictional_qr(second['qr_image'])
    assert old!=new
    with pytest.raises(PermissionDenied):qr.redeem(players[0],sid,old)
    with pytest.raises(PermissionDenied):qr.redeem(players[0],sid+100,new)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:qr.redeem(players[0],sid,new),range(2)))
    assert len({x[0] for x in results})==1 and sorted(x[1] for x in results)==[False,True]
    service.mark_no_show(owner,sid,players[1])
    with pytest.raises(PermissionDenied):qr.redeem(players[1],sid,new)
    world.state.clock.t+=179
    assert qr.redeem(players[2],sid,new)[1] is True
    world.state.clock.t+=1
    with pytest.raises(PermissionDenied):qr.redeem(players[3],sid,new)
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM checkins WHERE session_id=?',(sid,)).fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM queue_entries WHERE session_id=? AND state='waiting'",(sid,)).fetchone()[0]==2
