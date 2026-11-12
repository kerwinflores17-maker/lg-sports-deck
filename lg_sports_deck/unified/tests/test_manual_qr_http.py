"""Opt-in fictional-only UI/API payment review. No real accounts or networks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
import os

import pytest

from lg_sports_deck.unified.app import create_app
from lg_sports_deck.unified.tests.test_unified import (
    ORIGIN, PW, Clock, client, create_session, create_staff, grants, login, notice,
    register, world,
)


@pytest.fixture
def manual_world(tmp_path):
    clock=Clock()
    app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'fictional-media',{ORIGIN},clock=clock,
                   enable_fictional_manual_qr=True,ffmpeg_path=os.environ.get('FFMPEG_TEST_BIN'))
    owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Fictional Owner',PW)
    app.state.unified.set_limits(owner,4,12)
    app.state.owner=owner;app.state.clock=clock;app.state.test_db=tmp_path/'fictional.sqlite3'
    yield app
    app.state.recorder.close()


def request_open(player,headers,sid,uid,key='fictional_key_123456789'):
    return player.post('/api/u/test-manual-qr/openplay',headers=headers,json={
        'idempotency_key':key,'session_id':sid,'player_ids':[uid]})


def test_manual_routes_are_opt_in_and_real_references_are_never_accepted(world):
    with client(world) as c:
        h=login(c,'owner@example.invalid')
        assert c.get('/api/u/catalog').json()['fictional_manual_qr_enabled'] is False
        assert c.get('/api/u/test-manual-qr/review').status_code==404
        assert c.post('/api/u/test-manual-qr/1/review',headers=h,json={}).status_code==404


def test_end_to_end_claim_and_independent_staff_check(manual_world,monkeypatch):
    # The application must not invoke a network mail transport in this flow.
    import socket, smtplib
    def fail(*args,**kwargs):raise AssertionError('No email or external connection is allowed')
    monkeypatch.setattr(socket,'create_connection',fail)
    monkeypatch.setattr(smtplib,'SMTP',fail);monkeypatch.setattr(smtplib,'SMTP_SSL',fail)
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid')
        sid=create_session(owner,oh)
        aid=create_staff(owner,oh,'reviewer@example.invalid','admin')
        reviewer=stack.enter_context(client(app));rh=login(reviewer,'reviewer@example.invalid')
        player=stack.enter_context(client(app));ph,pid=register(player,'buyer@example.invalid','Fictional Buyer');notice(player,ph)
        outsider=stack.enter_context(client(app));xh,xid=register(outsider,'other@example.invalid','Other Player');notice(outsider,xh)
        app.state.test_integration_settings.replace_booking_recipient(app.state.owner,0,'venue@example.invalid')

        assert player.get('/api/u/catalog').json()['fictional_manual_qr_enabled'] is True
        assert player.post('/api/u/book/openplay',headers=ph,json={'session_id':sid,'player_ids':[pid]}).status_code==409
        assert player.post('/api/u/book/court',headers=ph,json={'day':'2027-06-02','start_hour':12,'duration':1,'court':1,'player_ids':[pid]}).status_code==409
        request=request_open(player,ph,sid,pid);assert request.status_code==200,request.text
        initial=request.json();intent=initial['intent_id']
        assert initial['state']=='pending' and initial['amount']==200 and initial['booking_id'] is None
        assert request_open(player,ph,sid,pid).json()['intent_id']==intent
        assert request_open(player,ph,sid,pid,'fictional_key_222222222').json()['state']=='pending'
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(sid,)).fetchone()[0]==0
        assert player.get('/api/u/my-bookings').json()['bookings']==[]
        assert owner.get('/api/u/ops/sales').json()['total']==0
        assert reviewer.get('/api/u/test-manual-qr/review').status_code==403
        assert player.get('/api/u/test-manual-qr/review').status_code==403
        assert outsider.get('/api/u/test-manual-qr/mine').json()['requests']==[]
        path=f'/api/u/test-manual-qr/{intent}'
        assert outsider.post(path+'/claim',headers=xh,json={'reference':'FAKE-QR-sample0001'}).status_code==403
        assert owner.post(path+'/claim',headers=oh,json={'reference':'FAKE-QR-sample0001'}).status_code==403
        assert player.post(path+'/claim',headers=ph,json={'reference':'123456789012'}).status_code==422
        assert player.post(path+'/claim',headers=ph,json={'reference':'FAKE-QR-sample0001','receipt':'do-not-upload'}).status_code==422
        assert grants(owner,oh,aid,['payments.manual_verify']).status_code==200
        queue=reviewer.get('/api/u/test-manual-qr/review').json()
        item=next(q for q in queue['requests'] if q['intent_id']==intent)
        assert item['test_booker_name']=='Fictional Buyer' and not item['claim_submitted']
        good={'reference':'FAKE-QR-sample0001','observed_amount':200,'account_checked':True}
        assert reviewer.post(path+'/review',headers=rh,json=good).status_code==409
        claim=player.post(path+'/claim',headers=ph,json={'reference':good['reference']})
        assert claim.status_code==200 and claim.json()['state']=='pending' and claim.json()['claim_suffix']=='0001'
        assert player.post(path+'/claim',headers=ph,json={'reference':good['reference']}).status_code==200
        with app.state.engine.db.read() as c:
            claims=list(c.execute('SELECT * FROM unified_test_manual_qr_claims'))
            assert len(claims)==1 and 'FAKE-QR-sample0001' not in str([dict(x) for x in claims])
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
        assert reviewer.post(path+'/review',headers=rh,json={**good,'reference':'FAKE-QR-incorrect1'}).status_code==409
        assert reviewer.post(path+'/review',headers=rh,json={**good,'account_checked':False}).status_code==422
        assert reviewer.post(path+'/review',headers=rh,json={**good,'account_checked':'true'}).status_code==422
        assert reviewer.post(path+'/review',headers=rh,json={**good,'observed_amount':True}).status_code==422
        done=reviewer.post(path+'/review',headers=rh,json=good)
        assert done.status_code==200 and done.json()['state']=='confirmed' and done.json()['booking_id']
        assert reviewer.post(path+'/review',headers=rh,json=good).json()['booking_id']==done.json()['booking_id']
        assert player.post(path+'/claim',headers=ph,json={'reference':good['reference']}).status_code==409
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
            jobs=[dict(r) for r in c.execute('SELECT recipient,state FROM unified_test_mail_jobs')]
            assert {j['recipient'] for j in jobs}=={'buyer@example.invalid','venue@example.invalid'}
            assert all(j['state']=='queued' for j in jobs)
            assert c.execute('SELECT count(*) FROM auth_audit WHERE event=? AND actor_id=?',
                ('unified.fictional_manual_qr_reviewed',aid)).fetchone()[0]==1
        assert player.get('/api/u/my-bookings').json()['bookings'][0]['id']==done.json()['booking_id']
        assert grants(owner,oh,aid,[]).status_code==200
        assert reviewer.get('/api/u/test-manual-qr/review').status_code==403
        assert reviewer.post(path+'/review',headers=rh,json=good).status_code==403


def test_duplicate_claim_can_be_submitted_but_cannot_confirm_twice(manual_world):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        buyers=[]
        for i in range(2):
            c=stack.enter_context(client(app));h,uid=register(c,f'buyer{i}@example.invalid',f'Fictional {i}');notice(c,h)
            r=request_open(c,h,sid,uid,f'fictional_key_{i}123456789');assert r.status_code==200
            iid=r.json()['intent_id'];assert c.post(f'/api/u/test-manual-qr/{iid}/claim',headers=h,json={'reference':'FAKE-QR-samevalue1'}).status_code==200
            buyers.append(iid)
        data={'reference':'FAKE-QR-samevalue1','observed_amount':200,'account_checked':True}
        assert owner.post(f'/api/u/test-manual-qr/{buyers[0]}/review',headers=oh,json=data).json()['state']=='confirmed'
        second=owner.post(f'/api/u/test-manual-qr/{buyers[1]}/review',headers=oh,json=data)
        assert second.status_code==409
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
            assert c.execute('SELECT state FROM unified_test_manual_qr_intents WHERE id=?',(buyers[1],)).fetchone()[0]=='pending'


def test_changed_claim_is_checked_at_review_and_mismatch_does_not_reserve_other_reference(manual_world):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        buyer=stack.enter_context(client(app));bh,uid=register(buyer,'buyer@example.invalid','Fictional Buyer');notice(buyer,bh)
        iid=request_open(buyer,bh,sid,uid).json()['intent_id'];path=f'/api/u/test-manual-qr/{iid}'
        assert buyer.post(path+'/claim',headers=bh,json={'reference':'FAKE-QR-first1111'}).status_code==200
        assert buyer.post(path+'/claim',headers=bh,json={'reference':'FAKE-QR-newer2222'}).status_code==200
        assert owner.post(path+'/review',headers=oh,json={'reference':'FAKE-QR-first1111','observed_amount':200,'account_checked':True}).status_code==409
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_test_manual_qr_claims').fetchone()[0]==2
            row=c.execute('SELECT state,reference_hash FROM unified_test_manual_qr_intents WHERE id=?',(iid,)).fetchone()
            assert tuple(row)==('pending',None)
        assert owner.post(path+'/review',headers=oh,json={'reference':'FAKE-QR-newer2222','observed_amount':200,'account_checked':True}).json()['state']=='confirmed'


def test_amount_mismatch_is_preserved_for_review_and_cannot_create_booking(manual_world):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        buyer=stack.enter_context(client(app));bh,uid=register(buyer,'buyer@example.invalid','Fictional Buyer');notice(buyer,bh)
        iid=request_open(buyer,bh,sid,uid).json()['intent_id'];path=f'/api/u/test-manual-qr/{iid}'
        assert buyer.post(path+'/claim',headers=bh,json={'reference':'FAKE-QR-mismatch1'}).status_code==200
        wrong={'reference':'FAKE-QR-mismatch1','observed_amount':199,'account_checked':True}
        result=owner.post(path+'/review',headers=oh,json=wrong).json()
        assert result['state']=='manual_review' and result['issue']=='amount_mismatch' and result['booking_id'] is None
        assert owner.post(path+'/review',headers=oh,json={**wrong,'observed_amount':200}).status_code==409
        assert any(x['intent_id']==iid and x['issue']=='amount_mismatch' for x in owner.get('/api/u/test-manual-qr/review').json()['requests'])
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0


def test_concurrent_fictional_court_reviews_do_not_oversell(manual_world):
    app=manual_world;owner=app.state.owner;lab=app.state.test_manual_qr;accounts=app.state.accounts
    people=[]
    for i in range(2):
        p=accounts.register_test_player(f'c{i}@example.invalid',f'Player {i}',PW);accounts.accept_test_notice(p);people.append(p)
    ids=[]
    for i,p in enumerate(people):
        result=lab.initiate(p,f'fictional_key_{i}123456789','court',{'day':'2027-06-02','start_hour':12,'duration':2,'court':1,'player_ids':[p]})
        ref=f'FAKE-QR-courttest{i}';lab.submit_claim(p,result['intent_id'],ref);ids.append((result['intent_id'],ref))
    def verify(x):return lab.confirm_account_check(owner,x[0],x[1],500,account_checked=True)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(verify,ids))
    assert sorted(r['state'] for r in results)==['confirmed','manual_review']
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_test_manual_qr_intents WHERE reference_hash IS NOT NULL').fetchone()[0]==2


def test_csrf_and_restart_fail_closed_with_pending_requests(manual_world,tmp_path):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        buyer=stack.enter_context(client(app));bh,uid=register(buyer,'buyer@example.invalid','Fictional Buyer');notice(buyer,bh)
        payload={'idempotency_key':'fictional_key_123456789','session_id':sid,'player_ids':[uid]}
        assert buyer.post('/api/u/test-manual-qr/openplay',headers={'Origin':ORIGIN},json=payload).status_code==403
        assert buyer.post('/api/u/test-manual-qr/openplay',headers={**bh,'Origin':'https://attacker.invalid'},json=payload).status_code==403
        iid=request_open(buyer,bh,sid,uid).json()['intent_id'];path=f'/api/u/test-manual-qr/{iid}'
        assert buyer.post(path+'/claim',headers={'Origin':ORIGIN},json={'reference':'FAKE-QR-sample0001'}).status_code==403
        assert owner.post(path+'/review',headers={**oh,'Origin':'https://attacker.invalid'},json={'reference':'FAKE-QR-sample0001','observed_amount':200,'account_checked':True}).status_code==403
        assert buyer.get('/api/u/test-manual-qr/mine').headers['cache-control']=='no-store'
    with pytest.raises(RuntimeError,match='Outstanding fictional manual-QR'):
        create_app(app.state.test_db,tmp_path/'other-test-media',{ORIGIN},clock=app.state.clock)
    resumed=create_app(app.state.test_db,tmp_path/'resumed-test-media',{ORIGIN},clock=app.state.clock,enable_fictional_manual_qr=True)
    with client(resumed) as c:
        login(c,'buyer@example.invalid')
        assert c.get('/api/u/test-manual-qr/mine').json()['requests'][0]['intent_id']==iid
    resumed.state.recorder.close()


@pytest.mark.parametrize('role',['admin','manager'])
def test_each_http_staff_role_is_default_deny_and_revocable(manual_world,role):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid')
        uid=create_staff(owner,oh,f'{role}@example.invalid',role)
        staff=stack.enter_context(client(app));login(staff,f'{role}@example.invalid')
        assert staff.get('/api/u/test-manual-qr/review').status_code==403
        assert grants(owner,oh,uid,['payments.manual_verify']).status_code==200
        assert staff.get('/api/u/test-manual-qr/review').status_code==200
        assert grants(owner,oh,uid,[]).status_code==200
        assert staff.get('/api/u/test-manual-qr/review').status_code==403


def test_group_request_remains_unbooked_until_matching_review(manual_world):
    app=manual_world
    with ExitStack() as stack:
        owner=stack.enter_context(client(app));oh=login(owner,'owner@example.invalid');sid=create_session(owner,oh)
        players=[]
        for i in range(4):
            c=stack.enter_context(client(app));h,p=register(c,f'group{i}@example.invalid',f'Group player {i}');notice(c,h)
            players.append((c,h,p))
        ids=[p for _,_,p in players]
        body={'idempotency_key':'fictional_groupkey_001','session_id':sid,'player_ids':ids}
        request=players[0][0].post('/api/u/test-manual-qr/openplay',headers=players[0][1],json=body)
        assert request.status_code==200 and request.json()['amount']==800
        iid=request.json()['intent_id'];path=f'/api/u/test-manual-qr/{iid}'
        assert players[1][0].post(path+'/claim',headers=players[1][1],json={'reference':'FAKE-QR-group2026'}).status_code==403
        assert all(c.get('/api/u/my-bookings').json()['bookings']==[] for c,_,_ in players)
        assert players[0][0].post(path+'/claim',headers=players[0][1],json={'reference':'FAKE-QR-group2026'}).status_code==200
        result=owner.post(path+'/review',headers=oh,json={'reference':'FAKE-QR-group2026','observed_amount':800,'account_checked':True})
        assert result.status_code==200 and result.json()['state']=='confirmed'
        assert all(len(c.get('/api/u/my-bookings').json()['bookings'])==1 for c,_,_ in players)
        with app.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(sid,)).fetchone()[0]==4
            jobs=[dict(row) for row in c.execute('SELECT recipient,state FROM unified_test_mail_jobs')]
            assert len(jobs)==1 and jobs[0]=={'recipient':'group0@example.invalid','state':'queued'}
