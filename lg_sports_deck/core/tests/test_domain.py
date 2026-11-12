from pathlib import Path
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from lg_sports_deck.core.domain import Database, Engine, PermissionDenied, Conflict, verify_password
from lg_sports_deck.core.recording import RecordingCoordinator, SyntheticFFmpegBackend


class FakeBackend:
    """Not a playable video: used only for deterministic coordinator unit tests."""
    def __init__(self): self.started=[];self.stopped=[];self.aborted=[]
    def start(self,court_id,match_id,destination):
        h={'court':court_id,'match':match_id,'path':destination,'alive':True};self.started.append(h);return h
    def stop(self,handle):
        handle['alive']=False;self.stopped.append(handle['match']);handle['path'].parent.mkdir(parents=True,exist_ok=True);handle['path'].write_bytes(b'SYNTHETIC INTERNAL TEST ONLY');return handle['path']
    def abort(self,handle): handle['alive']=False;self.aborted.append(handle['match'])


class FailingFirstStop(FakeBackend):
    def stop(self,handle):
        if not self.stopped:
            self.stopped.append(handle['match']);raise RuntimeError('test backend failure')
        return super().stop(handle)


@pytest.fixture
def world(tmp_path):
    engine=Engine(Database(tmp_path/'domain.db'))
    pw='FictionalTest123!'
    su=engine.create_fixture_user('owner@example.invalid','Test Superuser',pw,'superuser')
    admin=engine.create_fixture_user('admin@example.invalid','Test Admin',pw,'admin')
    manager=engine.create_fixture_user('manager@example.invalid','Test Manager',pw,'manager')
    players=[engine.create_fixture_user(f'player{i}@example.invalid',f'Test Player {i}',pw) for i in range(1,13)]
    sid=engine.create_session(su,'Fictional session',venue_qr_token='test-venue-token',capacity=22)
    for p in players: engine.fixture_test_payment(su,sid,p)
    return {'e':engine,'su':su,'admin':admin,'manager':manager,'players':players,'sid':sid,'path':tmp_path,'pw':pw}


def checkin(world,count=12):
    for player in world['players'][:count]:world['e'].check_in(world['sid'],player,'test-venue-token')


def by_team(match,team):return [p['user_id'] for p in match['players'] if p['team']==team]


def test_fixture_accounts_hash_password_and_superuser_alone_grants(world):
    e=world['e'];su=world['su'];admin=world['admin'];manager=world['manager']
    with e.db.read() as c: encoded=c.execute('SELECT password_hash FROM users WHERE id=?',(world['players'][0],)).fetchone()[0]
    assert world['pw'] not in encoded and verify_password(world['pw'],encoded) and not verify_password('wrong password',encoded)
    with pytest.raises(ValueError):e.create_fixture_user('someone@gmail.com','Real address not accepted',world['pw'])
    e.grant_staff_permission(su,admin,'queue.manage')
    with pytest.raises(PermissionDenied):e.grant_staff_permission(admin,manager,'queue.manage')
    with pytest.raises(ValueError):e.grant_staff_permission(su,admin,'recordings.view_all')


def test_checkin_requires_test_paid_registration_and_venue_token_orders_queue(world):
    e=world['e'];p=world['players'];sid=world['sid']
    outsider=e.create_fixture_user('outsider@example.invalid','Unregistered',world['pw'])
    with pytest.raises(PermissionDenied):e.check_in(sid,p[0],'wrong-token')
    with pytest.raises(PermissionDenied):e.check_in(sid,outsider,'test-venue-token')
    order=[p[2],p[0],p[3],p[1]]
    ids=[e.check_in(sid,x,'test-venue-token') for x in order]
    assert e.check_in(sid,p[2],'test-venue-token')==ids[0]
    assert [r['user_id'] for r in e.queue(sid)]==order
    mid=e.assign_next(world['su'],sid,1)
    assert set(x['user_id'] for x in e.match(mid)['players'])==set(order)


def test_queue_return_without_score_and_both_team_confirmations(world):
    e=world['e'];checkin(world,4);mid=e.assign_next(world['su'],world['sid'],1);m=e.match(mid)
    e.end_match(by_team(m,'A')[0],mid)
    assert len(e.queue(world['sid']))==4 and {x['classification'] for x in e.queue(world['sid'])}=={'pending'}
    assert e.ranking()==[]
    # Missing score does not block a subsequent match in this provisional test algorithm.
    new_match=e.assign_next(world['su'],world['sid'],2)
    assert len(e.match(new_match)['players'])==4
    e.grant_staff_permission(world['su'],world['admin'],'score.enter')
    e.submit_score(world['admin'],mid,11,9)
    with pytest.raises(PermissionDenied):e.confirm_score(world['admin'],mid)
    assert e.confirm_score(by_team(m,'A')[0],mid) is False
    assert e.ranking()==[] and e.outbox(mid,'result')==[]
    assert e.confirm_score(by_team(m,'B')[0],mid) is True
    stats={r['user_id']:r for r in e.ranking()}
    assert all(stats[p]['wins']==1 and stats[p]['win_rate']==100 for p in by_team(m,'A'))
    assert all(stats[p]['losses']==1 and stats[p]['win_rate']==0 for p in by_team(m,'B'))
    assert len(e.outbox(mid,'result'))==4 and all(x['state']=='test_only' for x in e.outbox(mid,'result'))
    with pytest.raises(Conflict):e.submit_score(world['admin'],mid,11,8)


def test_confirmed_winners_and_losers_group_together(world):
    e=world['e'];checkin(world,8)
    m1=e.assign_next(world['su'],world['sid'],1);m2=e.assign_next(world['su'],world['sid'],2)
    winners=[];losers=[]
    for mid in (m1,m2):
        m=e.match(mid);a=by_team(m,'A');b=by_team(m,'B');winners+=a;losers+=b
        e.end_match(a[0],mid);e.submit_score(a[0],mid,11,7);e.confirm_score(a[0],mid);e.confirm_score(b[0],mid)
    assert {q['classification'] for q in e.queue(world['sid'])}=={'winner','loser'}
    next1=e.assign_next(world['su'],world['sid'],1)
    next2=e.assign_next(world['su'],world['sid'],2)
    sets=[{p['user_id'] for p in e.match(m)['players']} for m in (next1,next2)]
    assert {frozenset(s) for s in sets}=={frozenset(winners),frozenset(losers)}


def test_same_court_rollover_independent_other_court_private_replay_and_mail(world):
    e=world['e'];checkin(world,12);su=world['su']
    m1=e.assign_next(su,world['sid'],1);m2=e.assign_next(su,world['sid'],2)
    p1=e.match(m1)['players'][0]['user_id'];p2=e.match(m2)['players'][0]['user_id']
    backend=FakeBackend();rc=RecordingCoordinator(e,backend,world['path']/'private-media')
    try:
        r1=rc.start(p1,m1);r2=rc.start(p2,m2)
        assert rc.start(p1,m1)['already_running'] is True
        with pytest.raises(PermissionDenied):rc.start(world['players'][11],m1)
        e.end_match(p1,m1)
        m3=e.assign_next(su,world['sid'],1);p3=e.match(m3)['players'][0]['user_id']
        r3=rc.start(p3,m3)
        assert not r3['previous_failed'] and backend.stopped==[m1]
        assert next(x for x in backend.started if x['match']==m2)['alive'] is True
        assert next(x for x in backend.started if x['match']==m3)['alive'] is True
        with e.db.read() as c:
            states={r['match_id']:r['state'] for r in c.execute('SELECT match_id,state FROM recordings')}
        assert states=={m1:'ready',m2:'recording',m3:'recording'}
        assert len(e.outbox(m1,'replay'))==4 and all('pending' in x['body'] or 'missing' in x['body'] for x in e.outbox(m1,'replay'))
        assert e.outbox(m1,'result')==[]
        assert e.private_recording(p1,m1).is_file() and e.private_recording(su,m1).is_file()
        for unauthorized in (world['admin'],world['manager'],world['players'][11]):
            with pytest.raises(PermissionDenied):e.private_recording(unauthorized,m1)
        # A separate later result update goes to each participant after both confirmations.
        match=e.match(m1);a=by_team(match,'A');b=by_team(match,'B')
        e.submit_score(a[0],m1,11,9);e.confirm_score(a[0],m1);assert e.outbox(m1,'result')==[]
        e.confirm_score(b[0],m1);assert len(e.outbox(m1,'result'))==4
    finally: rc.close()


def test_rollover_failure_is_not_falsely_marked_ready(world):
    e=world['e'];checkin(world,8);mid=e.assign_next(world['su'],world['sid'],1)
    p=e.match(mid)['players'][0]['user_id'];backend=FailingFirstStop();rc=RecordingCoordinator(e,backend,world['path']/'private')
    try:
        rc.start(p,mid);e.end_match(p,mid);nxt=e.assign_next(world['su'],world['sid'],1)
        p2=e.match(nxt)['players'][0]['user_id'];result=rc.start(p2,nxt)
        assert result['previous_failed'] is True
        with e.db.read() as c: old=c.execute('SELECT state FROM recordings WHERE match_id=?',(mid,)).fetchone()
        assert old['state']=='failed' and e.outbox(mid,'replay')==[]
    finally:rc.close()


def test_duplicate_start_is_serialized_within_one_coordinator(world):
    e=world['e'];checkin(world,4);mid=e.assign_next(world['su'],world['sid'],1)
    actors=[p['user_id'] for p in e.match(mid)['players']];backend=FakeBackend();rc=RecordingCoordinator(e,backend,world['path']/'private')
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda p:rc.start(p,mid),actors))
        assert len({r['recording_id'] for r in results})==1 and len(backend.started)==1
        assert sum(not r['already_running'] for r in results)==1
    finally:rc.close()


def test_rental_player_can_control_recording_but_no_rank_or_assumed_email(world):
    e=world['e'];players=world['players'][:4];mid=e.create_test_rental_match(world['su'],1,players)
    backend=FakeBackend();rc=RecordingCoordinator(e,backend,world['path']/'private')
    try:
        rc.start(players[2],mid);result=rc.stop(players[3],mid)
        assert result['state']=='ready' and e.private_recording(players[0],mid).is_file()
        assert e.ranking()==[] and e.outbox(mid,'replay')==[] # rental email rule still unresolved
    finally:rc.close()


def test_real_playable_synthetic_video_same_court_rollover(world):
    executable=os.environ.get('FFMPEG_TEST_BIN')
    if not executable or not Path(executable).is_file(): pytest.skip('Optional FFmpeg synthetic-video fixture unavailable')
    e=world['e'];checkin(world,8);mid=e.assign_next(world['su'],world['sid'],1);p=e.match(mid)['players'][0]['user_id']
    rc=RecordingCoordinator(e,SyntheticFFmpegBackend(executable),world['path']/'video')
    try:
        rc.start(p,mid);time.sleep(.45);e.end_match(p,mid)
        nxt=e.assign_next(world['su'],world['sid'],1);p2=e.match(nxt)['players'][0]['user_id']
        rollover=rc.start(p2,nxt);assert not rollover['previous_failed']
        old=e.private_recording(p,mid);assert old.stat().st_size>512
        time.sleep(.45);assert rc.stop(p2,nxt)['state']=='ready';assert e.private_recording(p2,nxt).stat().st_size>512
    finally:rc.close()


def test_openplay_final_score_ends_at_first_win_by_two(world):
    e=world['e'];checkin(world,4);mid=e.assign_next(world['su'],world['sid'],1)
    p=e.match(mid)['players'][0]['user_id'];e.end_match(p,mid)
    for a,b in [(10,8),(11,10),(12,9),(15,0),(13,10),(11,11),(True,9)]:
        with pytest.raises(ValueError):e.submit_score(p,mid,a,b)
    for a,b in [(11,0),(11,9),(12,10),(13,11),(0,11)]:
        e.submit_score(p,mid,a,b)


def test_cannot_assign_same_players_to_two_active_matches(world):
    e=world['e'];p=world['players'][:4];checkin(world,4)
    second=e.create_session(world['su'],'Other fictional session','other-test-qr')
    for player in p:
        e.fixture_test_payment(world['su'],second,player)
        e.check_in(second,player,'other-test-qr')
    def assign(args):
        try:return e.assign_next(world['su'],*args)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        mids=list(pool.map(assign,[(world['sid'],1),(second,2)]))
    assert sum(x is not None for x in mids)==1
    with pytest.raises(Conflict):e.create_test_rental_match(world['su'],1 if mids[0] is None else 2,p)


def test_unknown_failed_abort_blocks_later_same_court_starts(world):
    class FailedAbort(FailingFirstStop):
        def abort(self,handle): raise RuntimeError('test: process state unknown')
    e=world['e'];checkin(world,8);mid=e.assign_next(world['su'],world['sid'],1)
    p=e.match(mid)['players'][0]['user_id'];backend=FailedAbort();rc=RecordingCoordinator(e,backend,world['path']/'private')
    try:
        rc.start(p,mid);e.end_match(p,mid);new=e.assign_next(world['su'],world['sid'],1)
        p2=e.match(new)['players'][0]['user_id']
        with pytest.raises(Conflict):rc.start(p2,new)
        with e.db.read() as c: assert c.execute('SELECT state FROM recordings WHERE match_id=?',(mid,)).fetchone()[0]=='interrupted'
        with pytest.raises(Conflict):rc.start(p2,new)
        assert len(backend.started)==1 and e.outbox(mid,'replay')==[]
    finally:rc.close()


def test_fictional_retention_denies_expired_access_and_deletes_only_due_clip(world):
    from datetime import datetime,timedelta,timezone
    from lg_sports_deck.core.retention import TestRetention
    e=world['e'];checkin(world,8);m1=e.assign_next(world['su'],world['sid'],1);m2=e.assign_next(world['su'],world['sid'],2)
    p1=e.match(m1)['players'][0]['user_id'];p2=e.match(m2)['players'][0]['user_id']
    media=world['path']/'private';rc=RecordingCoordinator(e,FakeBackend(),media)
    try:
        r1=rc.start(p1,m1)['recording_id'];r2=rc.start(p2,m2)['recording_id']
        rc.stop(p1,m1);rc.stop(p2,m2);file1=e.private_recording(p1,m1);file2=e.private_recording(p2,m2)
        retain=TestRetention(e,media);now=datetime.now(timezone.utc)
        # No trigger has been chosen: an unscheduled test clip is not silently deleted.
        assert retain.purge_due_test_clips(now)=={'deleted':0,'failed':0} and file1.is_file()
        retain.set_fictional_anchor(r1,now-timedelta(days=6))
        retain.set_fictional_anchor(r2,now-timedelta(days=8))
        with pytest.raises(Conflict):e.private_recording(p2,m2)  # deny even before cleanup runs
        assert e.private_recording(p1,m1).is_file()
        assert retain.purge_due_test_clips(now)=={'deleted':1,'failed':0}
        assert file1.is_file() and not file2.exists()
        with pytest.raises(Conflict):e.private_recording(p2,m2)
        assert retain.purge_due_test_clips(now)=={'deleted':0,'failed':0}
        with pytest.raises(Conflict):retain.set_fictional_anchor(r1,now)
        with pytest.raises(ValueError):retain.set_fictional_anchor(r1,datetime.now())
    finally:rc.close()


def test_fictional_retention_refuses_symlink_and_keeps_expired_access_denied(world):
    from datetime import datetime,timedelta,timezone
    from lg_sports_deck.core.retention import TestRetention
    e=world['e'];checkin(world,4);m=e.assign_next(world['su'],world['sid'],1);p=e.match(m)['players'][0]['user_id']
    media=world['path']/'private';rc=RecordingCoordinator(e,FakeBackend(),media)
    try:
        rid=rc.start(p,m)['recording_id'];rc.stop(p,m);file=e.private_recording(p,m)
        outside=world['path']/'do-not-delete.txt';outside.write_text('safe')
        file.unlink();file.symlink_to(outside)
        retention=TestRetention(e,media);now=datetime.now(timezone.utc)
        retention.set_fictional_anchor(rid,now-timedelta(days=7))
        assert retention.purge_due_test_clips(now)=={'deleted':0,'failed':1}
        assert outside.read_text()=='safe' and file.is_symlink()
        with pytest.raises(Conflict):e.private_recording(p,m)
    finally:rc.close()


def test_authorized_staff_can_choose_queued_players_and_partners(world):
    e=world['e'];checkin(world,8);p=world['players'];session=world['sid']
    with pytest.raises(PermissionDenied):e.assign_selected(p[0],session,1,[p[7],p[0]],[p[6],p[1]])
    e.grant_staff_permission(world['su'],world['admin'],'queue.manage')
    with pytest.raises(ValueError):e.assign_selected(world['admin'],session,1,[p[0],p[0]],[p[1],p[2]])
    mid=e.assign_selected(world['admin'],session,1,[p[7],p[0]],[p[6],p[1]])
    assert set(by_team(e.match(mid),'A'))=={p[7],p[0]}
    assert set(by_team(e.match(mid),'B'))=={p[6],p[1]}
    assert {q['user_id'] for q in e.queue(session)}=={p[2],p[3],p[4],p[5]}
    with pytest.raises(Conflict):e.assign_selected(world['admin'],session,2,[p[7],p[2]],[p[3],p[4]])
