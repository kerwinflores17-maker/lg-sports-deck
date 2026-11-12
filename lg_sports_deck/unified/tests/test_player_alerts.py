"""Fictional player queue/ranking/replay alerts: no live users or email."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
from pathlib import Path
import os
import smtplib
import socket
import sqlite3
import subprocess
import time

import pytest

from lg_sports_deck.core.domain import PermissionDenied
from lg_sports_deck.core.recording import RecordingCoordinator, SyntheticFFmpegBackend
from lg_sports_deck.unified.notifications import SimulatedMailWorker, render_test_mime
from lg_sports_deck.unified.player_alerts import FictionalPlayerAlerts
from lg_sports_deck.unified.tests.test_unified import world, client, login, fixture_players


DAY=date(2027,6,2)
TOKEN='fictional-alert-token'


def no_external_mail(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('Real or network email is forbidden in this test')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(smtplib,'SMTP',forbidden)
    monkeypatch.setattr(smtplib,'SMTP_SSL',forbidden)


def make_openplay(world):
    svc=world.state.unified;eng=world.state.engine;owner=world.state.owner
    players=fixture_players(world,5)
    sid=svc.create_session(owner,DAY,8,3,'Fictional notification session',TOKEN)
    svc.book_openplay(players[0],sid,players[:4])
    for p in players[:4]:eng.check_in(sid,p,TOKEN)
    return sid,players


class TinyFakeBackend:
    """No real media or network; just exercises event/transaction wiring."""
    def start(self,court_id,match_id,destination):return destination
    def stop(self,handle):handle.write_bytes(b'fictional clip, not actual CCTV');return handle
    def abort(self,handle):pass


class FailedBackend(TinyFakeBackend):
    def stop(self,handle):raise RuntimeError('fictional finalize failure')


def test_assignment_alerts_are_owned_readable_and_have_unsent_email_jobs(world,monkeypatch):
    no_external_mail(monkeypatch)
    sid,people=make_openplay(world);engine=world.state.engine;alerts=world.state.test_player_alerts
    assert all(alerts.list(p)['alerts']==[] for p in people)
    mid=engine.assign_next(world.state.owner,sid,1)
    rows=[alerts.list(p) for p in people]
    assert [x['unread_count'] for x in rows]==[1,1,1,1,0]
    assert all(x['alerts'][0]['kind']=='queue_assignment' and x['alerts'][0]['match_id']==mid for x in rows[:4])
    with engine.db.read() as c:
        jobs=[dict(r) for r in c.execute('SELECT * FROM unified_test_extra_mail_jobs ORDER BY id')]
        assert len(jobs)==4 and {r['user_id'] for r in jobs}==set(people[:4])
        assert all(r['state']=='queued' and r['recipient'].endswith('.invalid') for r in jobs)
    # Installs are additive, repeated initialization never duplicates events.
    FictionalPlayerAlerts(world.state.unified)
    assert all(alerts.list(p)['unread_count']==1 for p in people[:4])
    msg=render_test_mime(engine.db,jobs[0]['id'],'https://portal.example.invalid','sender@example.invalid',bucket='extra')
    assert msg['X-LG-Test-Only']=='yes' and msg['To']==jobs[0]['recipient']
    assert f'/#match-{mid}' in msg.get_content() and 'No actual email was sent' in msg.get_content()
    with pytest.raises(ValueError):render_test_mime(engine.db,jobs[0]['id'],'https://portal.example.invalid','sender@gmail.com',bucket='extra')
    with pytest.raises(ValueError):SimulatedMailWorker(engine.db,bucket='extra;DROP TABLE users')
    with ExitStack() as stack:
        anon=stack.enter_context(client(world));one=stack.enter_context(client(world))
        other=stack.enter_context(client(world));owner=stack.enter_context(client(world))
        assert anon.get('/api/u/player-alerts').status_code==401
        h1=login(one,'p0@example.invalid');h2=login(other,'p1@example.invalid');login(owner,'owner@example.invalid')
        r=one.get('/api/u/player-alerts');assert r.status_code==200 and r.headers['cache-control']=='no-store'
        alert_id=r.json()['alerts'][0]['id'];assert r.json()['unread_count']==1
        assert owner.get('/api/u/player-alerts').status_code==403
        assert other.post(f'/api/u/player-alerts/{alert_id}/read',headers=h2,json={}).status_code==403
        assert one.post(f'/api/u/player-alerts/{alert_id}/read',headers={'Origin':h1['Origin']},json={}).status_code==403
        assert one.post(f'/api/u/player-alerts/{alert_id}/read',headers={**h1,'Origin':'https://attacker.invalid'},json={}).status_code==403
        assert one.post(f'/api/u/player-alerts/{alert_id}/read',headers=h1,json={'user_id':people[1]}).status_code==422
        assert one.post(f'/api/u/player-alerts/{alert_id}/read',headers=h1,json={}).status_code==200
        assert one.post(f'/api/u/player-alerts/{alert_id}/read',headers=h1,json={}).status_code==200  # idempotent
        assert one.get('/api/u/player-alerts').json()['unread_count']==0
        assert other.get('/api/u/player-alerts').json()['unread_count']==1
        own_mail=one.get('/api/u/notices').json()['extra_test_mail']
        assert len(own_mail)==1 and own_mail[0]['kind']=='queue_assignment' and own_mail[0]['state']=='queued'
        assert one.post('/api/auth/logout',headers=h1).status_code==200
        assert one.get('/api/u/player-alerts').status_code==401
        login(one,'p0@example.invalid')
        assert one.get('/api/u/player-alerts').json()['unread_count']==0  # read state persisted


def test_video_before_score_and_ranking_only_after_both_teams_confirm(world,tmp_path,monkeypatch):
    no_external_mail(monkeypatch)
    sid,players=make_openplay(world);eng=world.state.engine;alerts=world.state.test_player_alerts
    mid=eng.assign_next(world.state.owner,sid,1)
    rc=RecordingCoordinator(eng,TinyFakeBackend(),tmp_path/'fictional-media')
    try:
        rc.start(players[0],mid);assert rc.stop(players[1],mid)['state']=='ready'
        assert rc.stop(players[1],mid)['already_stopped']
        for p in players[:4]:
            assert {a['kind'] for a in alerts.list(p)['alerts']}=={'queue_assignment','video_ready'}
            assert not any(a['kind']=='ranking_update' for a in alerts.list(p)['alerts'])
            assert sum(j['kind']=='replay' for j in world.state.unified.notices(p)['test_mail'])==1
        assert alerts.list(players[4])['alerts']==[]
        eng.end_match(players[0],mid)
        assert len(eng.queue(sid))==4  # queue return does not wait for a score
        eng.submit_score(players[0],mid,11,9)
        assert eng.confirm_score(players[0],mid) is False and eng.ranking()==[]
        assert all(not any(a['kind']=='ranking_update' for a in alerts.list(p)['alerts']) for p in players[:4])
        assert eng.confirm_score(players[2],mid) is True and eng.confirm_score(players[2],mid) is True
        for p in players[:4]:
            items=alerts.list(p)['alerts']
            assert {a['kind'] for a in items}=={'queue_assignment','video_ready','ranking_update'} and len(items)==3
            notices=world.state.unified.notices(p)
            assert sum(j['kind']=='result' for j in notices['test_mail'])==1
            assert 'Rankings' in next(j for j in notices['test_mail'] if j['kind']=='result')['body']
        assert len(eng.ranking())==4 and sum(row['wins'] for row in eng.ranking())==2
        with eng.db.read() as c:
            replay=c.execute("SELECT id FROM unified_test_mail_jobs WHERE user_id=? AND kind='replay'",(players[0],)).fetchone()[0]
            result=c.execute("SELECT id FROM unified_test_mail_jobs WHERE user_id=? AND kind='result'",(players[0],)).fetchone()[0]
        assert f'/#match-{mid}' in render_test_mime(eng.db,replay,'https://portal.example.invalid','sender@example.invalid').get_content()
        assert 'Rankings' in render_test_mime(eng.db,result,'https://portal.example.invalid','sender@example.invalid').get_content()
    finally:rc.close()


def test_failed_sample_recorder_does_not_announce_a_video(world,tmp_path):
    sid,players=make_openplay(world);eng=world.state.engine;mid=eng.assign_next(world.state.owner,sid,1)
    rc=RecordingCoordinator(eng,FailedBackend(),tmp_path/'failed-media')
    try:
        rc.start(players[0],mid);assert rc.stop(players[0],mid)['state']=='failed'
        assert all({a['kind'] for a in world.state.test_player_alerts.list(p)['alerts']}=={'queue_assignment'} for p in players[:4])
        assert all(not any(j['kind']=='replay' for j in world.state.unified.notices(p)['test_mail']) for p in players[:4])
    finally:rc.close()


def test_rental_sample_alert_is_for_match_participants_only_and_not_ranked(world,tmp_path,monkeypatch):
    no_external_mail(monkeypatch)
    players=fixture_players(world,5);svc=world.state.unified;eng=world.state.engine
    b=svc.book_court(players[0],DAY,12,1,2,players[:4])
    mid=svc.create_rental_match(players[0],b['booking_id'],players[:4])
    assert all(world.state.test_player_alerts.list(p)['alerts']==[] for p in players)
    rc=RecordingCoordinator(eng,TinyFakeBackend(),tmp_path/'rental-media')
    try:
        rc.start(players[3],mid);assert rc.stop(players[2],mid)['state']=='ready'
        for p in players[:4]:
            own=world.state.test_player_alerts.list(p)['alerts'];assert len(own)==1 and own[0]['kind']=='video_ready'
            jobs=svc.notices(p)['extra_test_mail'];assert len(jobs)==1 and jobs[0]['kind']=='rental_replay'
        assert world.state.test_player_alerts.list(players[4])['alerts']==[] and eng.ranking()==[]
        with pytest.raises(PermissionDenied):eng.private_recording(players[4],mid)
        with eng.db.read() as c:
            job=c.execute("SELECT id FROM unified_test_extra_mail_jobs WHERE kind='rental_replay' AND user_id=?",(players[0],)).fetchone()[0]
        msg=render_test_mime(eng.db,job,'https://portal.example.invalid','sender@example.invalid',bucket='extra')
        assert f'/#match-{mid}' in msg.get_content() and 'not your match footage' in msg.get_content()
    finally:rc.close()


def test_assignment_rolls_back_alerts_and_email_atomically(world):
    sid,players=make_openplay(world);eng=world.state.engine;db=eng.db
    with db.tx() as c:
        c.execute("""CREATE TRIGGER test_fail_assignment BEFORE UPDATE OF state ON queue_entries
                     WHEN NEW.state='playing' BEGIN SELECT RAISE(ABORT,'forced test rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError,match='forced test rollback'):
        eng.assign_next(world.state.owner,sid,1)
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM matches').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_player_alerts').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_extra_mail_jobs').fetchone()[0]==0
    with db.tx() as c:c.execute('DROP TRIGGER test_fail_assignment')
    mid=eng.assign_next(world.state.owner,sid,1)
    assert mid and all(world.state.test_player_alerts.list(p)['unread_count']==1 for p in players[:4])


def test_extra_mail_worker_isolated_claims_ambiguous_state_and_receipts(world,monkeypatch):
    no_external_mail(monkeypatch)
    sid,players=make_openplay(world);eng=world.state.engine;eng.assign_next(world.state.owner,sid,1)
    extra=SimulatedMailWorker(eng.db,world.state.clock,bucket='extra',lease_seconds=10)
    primary=SimulatedMailWorker(eng.db,world.state.clock,bucket='primary')
    claim=extra.claim();assert claim and claim.bucket=='extra'
    assert primary.finish(claim,'success')=='stale'  # same numeric IDs cannot cross buckets
    assert extra.finish(claim,'unknown')=='uncertain'
    with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(lambda _:extra.run_one(),range(5)))
    assert len([x for x in result if x])==3 and all(x[1]=='simulated' for x in result if x)
    with eng.db.read() as c:
        assert c.execute("SELECT count(*) FROM unified_test_extra_mail_jobs WHERE state='uncertain'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM unified_test_extra_mail_receipts').fetchone()[0]==3
        assert c.execute("SELECT count(*) FROM unified_test_mail_jobs WHERE state='queued'").fetchone()[0]==1  # player booking untouched


def test_additive_alert_upgrade_does_not_backfill_existing_matches_or_mail(world):
    sid,players=make_openplay(world);eng=world.state.engine;db=eng.db
    with db.tx() as c:
        for trigger in ('unified_test_player_assignment_alert','unified_test_player_openplay_alert','unified_test_player_rental_replay_alert'):
            c.execute(f'DROP TRIGGER {trigger}')
        before=[tuple(r) for r in c.execute('SELECT id,event_key,recipient,state FROM unified_test_mail_jobs ORDER BY id')]
    mid=eng.assign_next(world.state.owner,sid,1)
    FictionalPlayerAlerts(world.state.unified)
    with db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_test_player_alerts').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM unified_test_extra_mail_jobs').fetchone()[0]==0
        assert [tuple(r) for r in c.execute('SELECT id,event_key,recipient,state FROM unified_test_mail_jobs ORDER BY id')]==before
        assert c.execute('SELECT count(*) FROM matches WHERE id=?',(mid,)).fetchone()[0]==1


def test_labelled_stock_clip_is_playable_and_recorder_writes_a_separate_sample(tmp_path):
    ffmpeg=os.environ.get('FFMPEG_TEST_BIN')
    if not ffmpeg:pytest.skip('FFMPEG_TEST_BIN needed to verify fictional sample recording')
    sample=Path(__file__).resolve().parents[1]/'assets'/'sample-stock-match.mp4'
    assert sample.is_file() and sample.stat().st_size>10000
    backend=SyntheticFFmpegBackend(ffmpeg,sample_video=sample)
    target=tmp_path/'separate-sample.mp4';handle=backend.start(1,99,target)
    time.sleep(1.5)
    assert backend.stop(handle)==target and target.stat().st_size>1000
    assert target.read_bytes()!=sample.read_bytes()
    subprocess.run([ffmpeg,'-v','error','-i',str(target),'-f','null','-'],check=True,capture_output=True,timeout=20)


def test_staff_roles_and_disabled_players_cannot_read_player_alerts(world):
    from lg_sports_deck.unified.tests.test_unified import create_staff,grants
    sid,people=make_openplay(world);world.state.engine.assign_next(world.state.owner,sid,1)
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));oh=login(owner,'owner@example.invalid')
        for role in ('admin','manager'):
            email=f'{role}@example.invalid';uid=create_staff(owner,oh,email,role)
            assert grants(owner,oh,uid,owner.get('/api/staff').json()['grantable']).status_code==200
            staff=stack.enter_context(client(world));sh=login(staff,email)
            assert staff.get('/api/u/player-alerts').status_code==403
            assert staff.post('/api/u/player-alerts/1/read',headers=sh,json={}).status_code==403
        player=stack.enter_context(client(world));h=login(player,'p0@example.invalid')
        assert player.get('/api/u/player-alerts').status_code==200
        world.state.accounts.disable_test_user(world.state.owner,people[0])
        assert player.get('/api/u/player-alerts').status_code==401
        assert player.post('/api/u/player-alerts/1/read',headers=h,json={}).status_code==401


def test_real_sample_rollover_is_same_court_only(world,tmp_path):
    ffmpeg=os.environ.get('FFMPEG_TEST_BIN')
    if not ffmpeg:pytest.skip('FFMPEG_TEST_BIN needed for stock sample rollover')
    people=fixture_players(world,8);svc=world.state.unified;eng=world.state.engine;owner=world.state.owner
    svc.set_limits(owner,8,12)
    sid=svc.create_session(owner,DAY,8,3,'Fictional two-court session',TOKEN)
    svc.book_openplay(people[0],sid,people)
    for p in people:eng.check_in(sid,p,TOKEN)
    first=eng.assign_next(owner,sid,1);other=eng.assign_next(owner,sid,2)
    sample=Path(__file__).resolve().parents[1]/'assets'/'sample-stock-match.mp4'
    rc=RecordingCoordinator(eng,SyntheticFFmpegBackend(ffmpeg,sample_video=sample),tmp_path/'media')
    try:
        rc.start(people[0],first);rc.start(people[4],other);time.sleep(1.5)
        eng.end_match(people[0],first)
        next_match=eng.assign_next(owner,sid,1);rolled=rc.start(people[0],next_match)
        assert rolled['state']=='recording'
        with eng.db.read() as c:
            states={r['match_id']:r['state'] for r in c.execute('SELECT match_id,state FROM recordings')}
        assert states[first]=='ready'
        assert states[other]=='recording'  # not touched by Court 1
        time.sleep(1.2)
        assert rc.stop(people[0],next_match)['state']=='ready'
        assert rc.stop(people[4],other)['state']=='ready'
        clips=[eng.private_recording(people[0],first),eng.private_recording(people[0],next_match),
               eng.private_recording(people[4],other)]
        assert len(set(clips))==3 and all(p.is_file() and p.stat().st_size>1000 for p in clips)
        for clip in clips:
            subprocess.run([ffmpeg,'-v','error','-i',str(clip),'-f','null','-'],check=True,capture_output=True,timeout=20)
        with pytest.raises(PermissionDenied):eng.private_recording(people[0],other)
    finally:rc.close()


def test_older_unread_alerts_remain_reachable_beyond_display_limit(world):
    sid,people=make_openplay(world);eng=world.state.engine;mid=eng.assign_next(world.state.owner,sid,1)
    alerts=world.state.test_player_alerts;player=people[0]
    with eng.db.tx() as c:
        c.executemany('''INSERT INTO unified_test_player_alerts
          (event_key,user_id,kind,match_id,session_id,title,body,created_at)
          VALUES(?,?,'queue_assignment',?,?, '[TEST ONLY] Bulk alert','Fictional.',?)''',
          [(f'bulk-fixture:{i}',player,mid,sid,i) for i in range(100)])
    before=alerts.list(player);assert before['unread_count']==101 and len(before['alerts'])==100
    visible_ids={a['id'] for a in before['alerts']};assert 1 not in visible_ids
    alerts.mark_read(player,before['alerts'][0]['id'])
    after=alerts.list(player);assert after['unread_count']==100 and len(after['alerts'])==100
    assert 1 in {a['id'] for a in after['alerts']} and all(a['read_at'] is None for a in after['alerts'])


def test_ranking_alerts_and_result_jobs_roll_back_with_failed_confirmation(world):
    sid,people=make_openplay(world);eng=world.state.engine;mid=eng.assign_next(world.state.owner,sid,1)
    eng.end_match(people[0],mid);eng.submit_score(people[0],mid,11,9)
    assert not eng.confirm_score(people[0],mid)
    with eng.db.tx() as c:
        c.execute("""CREATE TRIGGER test_fail_result BEFORE INSERT ON outbox
                     WHEN NEW.kind='result' BEGIN SELECT RAISE(ABORT,'forced result rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError,match='forced result rollback'):
        eng.confirm_score(people[2],mid)
    assert eng.ranking()==[]
    with eng.db.read() as c:
        assert c.execute("SELECT score_state FROM matches WHERE id=?",(mid,)).fetchone()[0]=='pending'
        assert c.execute("SELECT count(*) FROM unified_test_player_alerts WHERE kind='ranking_update'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM unified_test_mail_jobs WHERE kind='result'").fetchone()[0]==0
    with eng.db.tx() as c:c.execute('DROP TRIGGER test_fail_result')
    assert eng.confirm_score(people[2],mid) and eng.confirm_score(people[2],mid)
    with eng.db.read() as c:
        assert c.execute("SELECT count(*) FROM unified_test_player_alerts WHERE kind='ranking_update'").fetchone()[0]==4
        assert c.execute("SELECT count(*) FROM unified_test_mail_jobs WHERE kind='result'").fetchone()[0]==4
