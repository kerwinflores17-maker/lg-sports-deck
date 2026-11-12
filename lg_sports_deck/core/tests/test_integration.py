"""Internal synthetic end-to-end checks, NOT a user-facing test site."""
from pathlib import Path
import os
import sqlite3
import tempfile
import time
import pytest
from fastapi.testclient import TestClient

# Use a completely separate staging database, never the running demo DB.
TEMP=tempfile.TemporaryDirectory(prefix='lg-integration-')
_prev_db=os.environ.get('LG_TEST_DB');_prev_secret=os.environ.get('LG_TEST_SESSION_SECRET')
os.environ['LG_TEST_DB']=str(Path(TEMP.name)/'staging.sqlite3')
os.environ['LG_TEST_SESSION_SECRET']='isolated-synthetic-integration-test-secret-only'
from lg_sports_deck.staging import app as stage
if _prev_db is None:os.environ.pop('LG_TEST_DB',None)
else:os.environ['LG_TEST_DB']=_prev_db
if _prev_secret is None:os.environ.pop('LG_TEST_SESSION_SECRET',None)
else:os.environ['LG_TEST_SESSION_SECRET']=_prev_secret

from lg_sports_deck.core.domain import Database, Engine, PermissionDenied, Conflict
from lg_sports_deck.core.staging_bridge import StagingBridge
from lg_sports_deck.core.recording import RecordingCoordinator, SyntheticFFmpegBackend


def sandbox_for_profile(profile_id):
    c=sqlite3.connect(str(stage.DB_PATH))
    try:return c.execute('SELECT sandbox_id FROM profiles WHERE id=?',(profile_id,)).fetchone()[0]
    finally:c.close()


def setup_flow(tmp_path, client):
    state=client.get('/api/bootstrap').json();heads={'X-CSRF-Token':state['csrf']}
    assert client.post('/api/demo/role',json={'role':'staff'},headers=heads).status_code==200
    created=client.post('/api/demo/sessions',json={'day':state['demo_day'],'start_hour':12,'duration':3},headers=heads)
    assert created.status_code==200
    session_id=created.json()['session_id']
    profiles=[p['id'] for p in state['profiles']]
    booking={'kind':'openplay','session_id':session_id,'player_ids':profiles,'demo_notice':True,'policy_notice':True}
    response=client.post('/api/demo/checkout',json=booking,headers=heads)
    assert response.status_code==200 and response.json()['amount']==800
    assert 'No real booking or payment' in response.json()['message']
    engine=Engine(Database(tmp_path/'domain.sqlite3'))
    superuser=engine.create_fixture_user('superuser@example.invalid','Test Superuser','FictionalPassword123!','superuser')
    bridge=StagingBridge(stage.DB_PATH,engine,superuser,sandbox_for_profile(state['actor']))
    mapped_session=bridge.register_session(session_id,'fictional-venue-token')
    return state,profiles,session_id,engine,superuser,bridge,mapped_session


def test_simulated_booking_to_checkin_match_recording_score_and_unsent_mail(tmp_path):
    executable=os.environ.get('FFMPEG_TEST_BIN')
    if not executable or not Path(executable).is_file():pytest.skip('Optional FFmpeg synthetic-video fixture unavailable')
    with TestClient(stage.app) as client:
        state,profiles,sid,e,su,bridge,domain_sid=setup_flow(tmp_path,client)
        with pytest.raises(PermissionDenied):bridge.check_in(sid,profiles[0],'wrong-token')
        # Check-in order, not booking order, determines the initial queue.
        order=[profiles[2],profiles[0],profiles[3],profiles[1]]
        mapped=[bridge.check_in(sid,p,'fictional-venue-token') for p in order]
        assert all(x['mode']=='simulated-only' for x in mapped)
        assert bridge.check_in(sid,order[0],'fictional-venue-token')['checkin_id']==mapped[0]['checkin_id']
        assert [x['user_id'] for x in e.queue(domain_sid)]==[x['user_id'] for x in mapped]
        mid=e.assign_next(su,domain_sid,1);match=e.match(mid)
        team_a=[p['user_id'] for p in match['players'] if p['team']=='A']
        team_b=[p['user_id'] for p in match['players'] if p['team']=='B']
        rc=RecordingCoordinator(e,SyntheticFFmpegBackend(executable),tmp_path/'private-synthetic-video')
        try:
            rc.start(team_a[0],mid);time.sleep(.4);e.end_match(team_a[0],mid)
            assert len(e.queue(domain_sid))==4 and e.ranking()==[]
            assert rc.stop(team_b[0],mid)['state']=='ready'
            assert e.private_recording(team_a[0],mid).stat().st_size>512
            replay=e.outbox(mid,'replay')
            assert len(replay)==4 and all(x['recipient'].endswith('.invalid') and x['state']=='test_only' for x in replay)
            assert all('missing' in x['body'] for x in replay) and e.outbox(mid,'result')==[]
            e.submit_score(team_a[0],mid,11,9)
            assert not e.confirm_score(team_a[0],mid) and not e.outbox(mid,'result')
            assert e.confirm_score(team_b[0],mid) and len(e.outbox(mid,'result'))==4
            assert {r['user_id'] for r in e.ranking() if r['wins']==1}==set(team_a)
            with pytest.raises(PermissionDenied):e.private_recording(999999,mid)
        finally:rc.close()


def test_bridge_rejects_other_sandbox_and_unbooked_session(tmp_path):
    with TestClient(stage.app) as one,TestClient(stage.app) as two:
        state,profiles,sid,e,su,bridge,domain_sid=setup_flow(tmp_path,one)
        other=two.get('/api/bootstrap').json()
        with pytest.raises(PermissionDenied):bridge.register_session(other['sessions'][0]['id'],'fictional-venue-token')
        with pytest.raises(PermissionDenied):bridge.check_in(sid,other['actor'],'fictional-venue-token')
        # Example A belongs to the first sandbox but its sample occupancy is not a player booking.
        other_session=state['sessions'][0]['id'];bridge.register_session(other_session,'another-token')
        with pytest.raises(PermissionDenied):bridge.check_in(other_session,profiles[0],'another-token')
        with pytest.raises(Conflict):StagingBridge(stage.DB_PATH,e,su,sandbox_for_profile(other['actor']))
