from contextlib import ExitStack
from pathlib import Path
import os
import time
import pytest
from fastapi.testclient import TestClient

from lg_sports_deck.accounts.portal import create_app
from lg_sports_deck.accounts.service import STAFF_GRANTABLE

ORIGIN='https://testserver'

@pytest.fixture
def app(tmp_path):
    server=create_app(tmp_path/'accounts.sqlite3',tmp_path/'media',{ORIGIN},secure_cookies=True,
                      ffmpeg_path=os.environ.get('FFMPEG_TEST_BIN'))
    owner=server.state.accounts.bootstrap_test_superuser('owner@example.invalid','Test Owner','Fictional owner password 42')
    session=server.state.engine.create_session(owner,'Account prototype session','fictional-venue-token',capacity=22)
    server.state.owner=owner;server.state.test_session=session
    yield server
    server.state.recorder.close()


def client(app):return TestClient(app,base_url=ORIGIN)
def anon(c):
    r=c.get('/api/auth/bootstrap');assert r.status_code==200
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf']}
def login(c,email,password):
    r=c.post('/api/auth/login',json={'email':email,'password':password},headers=anon(c))
    assert r.status_code==200,r.text
    return {'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf']},r

def register(c,email,name,password):
    return c.post('/api/auth/register',json={'email':email,'name':name,'password':password},headers=anon(c))

def myid(c):
    r=c.get('/api/auth/me');assert r.status_code==200;return r.json()['id']


def create_staff(owner,ownerh,email,role):
    r=owner.post('/api/staff',headers=ownerh,json={'email':email,'name':role.title()+' Test',
                 'password':'Fictional staff password 42','role':role})
    assert r.status_code==200,r.text;return r.json()['test_staff_id']


def test_login_cookie_csrf_origin_role_spoof_and_logout(app):
    with client(app) as c:
        assert c.get('/api/auth/me').status_code==401
        assert c.get('/api/auth/state').json()=={'authenticated':False}
        h=anon(c);payload={'email':'alice@example.invalid','name':'Alice Test','password':'Fictional Alice password 42'}
        assert c.post('/api/auth/register',json=payload).status_code==403
        assert c.post('/api/auth/register',headers={**h,'X-CSRF-Token':'wrong'},json=payload).status_code==403
        assert c.post('/api/auth/register',headers=h,json={**payload,'role':'superuser'}).status_code==422
        assert c.post('/api/auth/register',headers=h,json={**payload,'email':'real@gmail.com'}).status_code==422
        assert c.post('/api/auth/register',headers=h,json=payload).status_code==200
        headers,result=login(c,payload['email'],payload['password'])
        cookies=result.headers.get_list('set-cookie')
        auth=next(x for x in cookies if x.startswith('__Host-lg_session='))
        assert 'HttpOnly' in auth and 'Secure' in auth and 'SameSite=strict' in auth and 'Path=/' in auth
        me=c.get('/api/auth/me',headers={'X-User-Id':str(app.state.owner),'X-Role':'superuser'}).json()
        assert me['role']=='player' and me['email']==payload['email']
        assert c.post('/api/test/notice',headers={'Origin':ORIGIN}).status_code==403
        assert c.post('/api/test/notice',headers={**headers,'Origin':'https://attacker.invalid'}).status_code==403
        assert c.post('/api/test/notice',headers={**headers,'X-CSRF-Token':'wrong'}).status_code==403
        assert c.post('/api/test/notice',headers=headers).status_code==200
        assert c.get('/api/auth/me').json()['notice_acknowledged'] is True
        raw=c.cookies.get('__Host-lg_session')
        assert c.post('/api/auth/logout',headers=headers).status_code==200
        c.cookies.set('__Host-lg_session',raw,domain='testserver.local',path='/')
        assert c.get('/api/auth/me').status_code==401


def test_superuser_only_grants_and_revoke_applies_immediately(app):
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));admin=stack.enter_context(client(app));manager=stack.enter_context(client(app))
        oh,_=login(owner,'owner@example.invalid','Fictional owner password 42')
        aid=create_staff(owner,oh,'admin@example.invalid','admin')
        mid=create_staff(owner,oh,'manager@example.invalid','manager')
        ah,_=login(admin,'admin@example.invalid','Fictional staff password 42')
        mh,_=login(manager,'manager@example.invalid','Fictional staff password 42')
        assert admin.get('/api/ops/test-bookings').status_code==403
        assert admin.post('/api/ops/test-rate',headers=ah,json={'amount':220}).status_code==403
        assert admin.post(f'/api/staff/{aid}/permissions',headers=ah,json={'permissions':['queue.manage']}).status_code==403
        assert manager.get('/api/staff').status_code==403
        assert owner.post(f'/api/staff/{aid}/permissions',headers=oh,json={'permissions':['recordings.view']}).status_code==422
        permissions=['bookings.view','settings.rates.edit','queue.manage']
        assert owner.post(f'/api/staff/{aid}/permissions',headers=oh,json={'permissions':permissions}).status_code==200
        assert set(admin.get('/api/auth/me').json()['permissions'])==set(permissions)
        assert admin.get('/api/ops/test-bookings').status_code==200
        assert admin.post('/api/ops/test-rate',headers=ah,json={'amount':220}).status_code==200
        assert owner.post(f'/api/staff/{aid}/permissions',headers=oh,json={'permissions':[]}).status_code==200
        assert admin.get('/api/ops/test-bookings').status_code==403
        assert admin.post('/api/ops/test-rate',headers=ah,json={'amount':230}).status_code==403
        assert owner.post(f'/api/staff/{mid}/disable',headers=oh).status_code==200
        assert manager.get('/api/auth/me').status_code==401


def test_each_player_own_notice_group_booking_checkin_and_results(app):
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh,_=login(owner,'owner@example.invalid','Fictional owner password 42')
        aid=create_staff(owner,oh,'admin@example.invalid','admin')
        admin=stack.enter_context(client(app));ah,_=login(admin,'admin@example.invalid','Fictional staff password 42')
        people=[]
        for i in range(4):
            c=stack.enter_context(client(app));email=f'player{i}@example.invalid';password=f'Fictional player password {i} 42'
            assert register(c,email,f'Player {i}',password).status_code==200
            h,_=login(c,email,password);people.append((c,h,myid(c)))
        ids=[p[2] for p in people];sid=app.state.test_session
        for c,h,_ in people[:3]:assert c.post('/api/test/notice',headers=h).status_code==200
        # A player cannot attribute the notice to a different test account.
        assert people[0][0].post('/api/test/notice',headers=people[0][1],json={'user_id':ids[3]}).status_code==422
        with app.state.engine.db.read() as db:
            assert not db.execute('SELECT 1 FROM test_notices WHERE user_id=?',(ids[3],)).fetchone()
        booking={'session_id':sid,'player_ids':ids}
        assert people[0][0].post('/api/test/checkout',headers=people[0][1],json=booking).status_code==409
        with app.state.engine.db.read() as db:assert db.execute('SELECT count(*) FROM session_registrations').fetchone()[0]==0
        assert people[3][0].post('/api/test/notice',headers=people[3][1]).status_code==200
        result=people[0][0].post('/api/test/checkout',headers=people[0][1],json=booking)
        assert result.status_code==200 and result.json()['amount']==800 and result.json()['state']=='simulated_paid'
        assert people[0][0].post('/api/test/check-in',headers=people[0][1],json={'session_id':sid,'fictional_venue_token':'bad'}).status_code==403
        for c,h,_ in people:
            assert c.post('/api/test/check-in',headers=h,json={'session_id':sid,'fictional_venue_token':'fictional-venue-token'}).status_code==200
        assert [x['user_id'] for x in people[0][0].get(f'/api/test/queue/{sid}').json()['queue']]==ids
        assert admin.get(f'/api/test/queue/{sid}').status_code==403
        assert admin.post('/api/test/queue/assign',headers=ah,json={'session_id':sid,'court':1}).status_code==403
        assert owner.post(f'/api/staff/{aid}/permissions',headers=oh,json={'permissions':['queue.view','queue.manage','score.enter']}).status_code==200
        assert admin.get(f'/api/test/queue/{sid}').status_code==200
        assigned=admin.post('/api/test/queue/assign',headers=ah,json={'session_id':sid,'court':1});assert assigned.status_code==200
        match=assigned.json()['match_id'];m=app.state.engine.match(match)
        team_a=[p['user_id'] for p in m['players'] if p['team']=='A'];team_b=[p['user_id'] for p in m['players'] if p['team']=='B']
        assert admin.post(f'/api/test/matches/{match}/end',headers=ah).status_code==200
        assert len(people[0][0].get(f'/api/test/queue/{sid}').json()['queue'])==4
        assert admin.post(f'/api/test/matches/{match}/score',headers=ah,json={'score_a':11,'score_b':9}).status_code==200
        assert admin.post(f'/api/test/matches/{match}/confirm',headers=ah).status_code==403
        ca=next((c,h) for c,h,id in people if id==team_a[0]);cb=next((c,h) for c,h,id in people if id==team_b[0])
        assert ca[0].post(f'/api/test/matches/{match}/confirm',headers=ca[1]).json()['confirmed'] is False
        assert people[0][0].get('/api/test/ranking').json()['ranking']==[]
        assert cb[0].post(f'/api/test/matches/{match}/confirm',headers=cb[1]).json()['confirmed'] is True
        ranks=people[0][0].get('/api/test/ranking').json()['ranking'];assert len(ranks)==4 and sum(x['wins'] for x in ranks)==2
        assert all(x['kind']=='result' for x in people[0][0].get('/api/test/notices').json()['notices'])


def test_private_synthetic_replay_denied_to_admin_manager_and_nonparticipant(app):
    if not os.environ.get('FFMPEG_TEST_BIN'):pytest.skip('Synthetic FFmpeg fixture not enabled')
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh,_=login(owner,'owner@example.invalid','Fictional owner password 42')
        aid=create_staff(owner,oh,'admin@example.invalid','admin');mid=create_staff(owner,oh,'manager@example.invalid','manager')
        admin=stack.enter_context(client(app));ah,_=login(admin,'admin@example.invalid','Fictional staff password 42')
        manager=stack.enter_context(client(app));mh,_=login(manager,'manager@example.invalid','Fictional staff password 42')
        for staff in (aid,mid):
            assert owner.post(f'/api/staff/{staff}/permissions',headers=oh,json={'permissions':sorted(STAFF_GRANTABLE)}).status_code==200
        people=[]
        for i in range(5):
            c=stack.enter_context(client(app));email=f'video{i}@example.invalid';password=f'Fictional video password {i} 42'
            assert register(c,email,f'Video Player {i}',password).status_code==200
            h,_=login(c,email,password);assert c.post('/api/test/notice',headers=h).status_code==200
            people.append((c,h,myid(c)))
        ids=[p[2] for p in people[:4]];sid=app.state.test_session
        assert people[0][0].post('/api/test/checkout',headers=people[0][1],json={'session_id':sid,'player_ids':ids}).status_code==200
        for c,h,_ in people[:4]:
            assert c.post('/api/test/check-in',headers=h,json={'session_id':sid,'fictional_venue_token':'fictional-venue-token'}).status_code==200
        result=admin.post('/api/test/queue/assign',headers=ah,json={'session_id':sid,'court':1});assert result.status_code==200;match=result.json()['match_id']
        url=f'/api/test/matches/{match}'
        assert admin.post(url+'/recording/start',headers=ah).status_code==403
        assert manager.post(url+'/recording/start',headers=mh).status_code==403
        assert people[4][0].post(url+'/recording/start',headers=people[4][1]).status_code==403
        assert people[0][0].post(url+'/recording/start',headers=people[0][1]).status_code==200
        time.sleep(.45)
        assert people[1][0].post(url+'/recording/stop',headers=people[1][1]).json()['state']=='ready'
        assert admin.get(url+'/replay').status_code==403 and manager.get(url+'/replay').status_code==403
        assert people[4][0].get(url+'/replay',headers={'X-User-Id':str(ids[0]),'X-Role':'superuser'}).status_code==403
        assert owner.get(url+'/replay').status_code==200
        response=people[0][0].get(url+'/replay');assert response.status_code==200 and response.headers['content-type']=='video/mp4' and len(response.content)>512
        notices=people[0][0].get('/api/test/notices').json()['notices']
        assert len(notices)==1 and notices[0]['kind']=='replay' and 'missing' in notices[0]['body']


def test_password_change_revokes_other_http_sessions(app):
    with ExitStack() as stack:
        a=stack.enter_context(client(app));b=stack.enter_context(client(app))
        assert register(a,'p@example.invalid','Fictional Player','Fictional long password 42').status_code==200
        ah,_=login(a,'p@example.invalid','Fictional long password 42')
        bh,_=login(b,'p@example.invalid','Fictional long password 42')
        r=a.post('/api/auth/password',headers=ah,json={'old_password':'Fictional long password 42','new_password':'Changed fictional password 99'})
        assert r.status_code==200
        assert a.get('/api/auth/me').status_code==401 and b.get('/api/auth/me').status_code==401
        newh,_=login(b,'p@example.invalid','Changed fictional password 99')
        assert b.get('/api/auth/me').status_code==200


def test_http_rate_limit_applies_before_even_a_correct_password(app):
    with client(app) as c:
        assert register(c,'p@example.invalid','Fictional Player','Fictional long password 42').status_code==200
        for _ in range(5):
            r=c.post('/api/auth/login',headers=anon(c),json={'email':'p@example.invalid','password':'wrong fictional password'})
            assert r.status_code==401
        r=c.post('/api/auth/login',headers=anon(c),json={'email':'p@example.invalid','password':'Fictional long password 42'})
        assert r.status_code==429


def test_authenticated_rental_fixture_control_and_replay_privacy(app):
    if not os.environ.get('FFMPEG_TEST_BIN'):pytest.skip('Synthetic FFmpeg fixture not enabled')
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh,_=login(owner,'owner@example.invalid','Fictional owner password 42')
        aid=create_staff(owner,oh,'rental-admin@example.invalid','admin')
        admin=stack.enter_context(client(app));ah,_=login(admin,'rental-admin@example.invalid','Fictional staff password 42')
        assert owner.post(f'/api/staff/{aid}/permissions',headers=oh,json={'permissions':sorted(STAFF_GRANTABLE)}).status_code==200
        people=[]
        for i in range(5):
            c=stack.enter_context(client(app));email=f'rental{i}@example.invalid';password=f'Fictional rental password {i} 42'
            assert register(c,email,f'Rental {i}',password).status_code==200
            h,_=login(c,email,password);people.append((c,h,myid(c)))
        # This is a domain fixture, NOT an authenticated rental booking flow.
        match=app.state.engine.create_test_rental_match(app.state.owner,2,[p[2] for p in people[:4]])
        url=f'/api/test/matches/{match}'
        assert people[3][0].post(url+'/recording/start',headers=people[3][1]).status_code==200
        time.sleep(.45)
        assert people[2][0].post(url+'/recording/stop',headers=people[2][1]).json()['state']=='ready'
        assert people[0][0].get(url+'/replay').status_code==200
        assert people[4][0].get(url+'/replay').status_code==403
        assert admin.get(url+'/replay').status_code==403 and owner.get(url+'/replay').status_code==200
        assert app.state.engine.ranking()==[] and app.state.engine.outbox(match)==[]


def test_authenticated_same_court_rollover_does_not_stop_other_court(app):
    if not os.environ.get('FFMPEG_TEST_BIN'):pytest.skip('Synthetic FFmpeg fixture not enabled')
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh,_=login(owner,'owner@example.invalid','Fictional owner password 42')
        people=[]
        for i in range(8):
            c=stack.enter_context(client(app));email=f'court{i}@example.invalid';password=f'Fictional court password {i} 42'
            assert register(c,email,f'Court Player {i}',password).status_code==200
            h,_=login(c,email,password);assert c.post('/api/test/notice',headers=h).status_code==200
            people.append((c,h,myid(c)))
        ids=[p[2] for p in people];sid=app.state.test_session
        assert people[0][0].post('/api/test/checkout',headers=people[0][1],json={'session_id':sid,'player_ids':ids}).status_code==200
        for c,h,_ in people:
            assert c.post('/api/test/check-in',headers=h,json={'session_id':sid,'fictional_venue_token':'fictional-venue-token'}).status_code==200
        m1=owner.post('/api/test/queue/assign',headers=oh,json={'session_id':sid,'court':1}).json()['match_id']
        m2=owner.post('/api/test/queue/assign',headers=oh,json={'session_id':sid,'court':2}).json()['match_id']
        assert people[0][0].post(f'/api/test/matches/{m1}/recording/start',headers=people[0][1]).status_code==200
        assert people[4][0].post(f'/api/test/matches/{m2}/recording/start',headers=people[4][1]).status_code==200
        time.sleep(.45)
        assert people[0][0].post(f'/api/test/matches/{m1}/end',headers=people[0][1]).status_code==200
        m3=owner.post('/api/test/queue/assign',headers=oh,json={'session_id':sid,'court':1}).json()['match_id']
        next_players=[p['user_id'] for p in app.state.engine.match(m3)['players']]
        next_client=next((c,h) for c,h,p in people if p in next_players)
        started=next_client[0].post(f'/api/test/matches/{m3}/recording/start',headers=next_client[1]);assert started.status_code==200
        assert started.json()['previous_failed'] is False
        with app.state.engine.db.read() as db:
            state1=db.execute('SELECT state FROM recordings WHERE match_id=?',(m1,)).fetchone()[0]
            state2=db.execute('SELECT state FROM recordings WHERE match_id=?',(m2,)).fetchone()[0]
            assert state1=='ready' and state2=='recording'
        assert people[0][0].get(f'/api/test/matches/{m1}/replay').status_code==200
        time.sleep(.4)
        assert people[4][0].post(f'/api/test/matches/{m2}/recording/stop',headers=people[4][1]).json()['state']=='ready'
        assert next_client[0].post(f'/api/test/matches/{m3}/recording/stop',headers=next_client[1]).json()['state']=='ready'
