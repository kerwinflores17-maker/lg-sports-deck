"""Opt-in fictional sample cleanup. Never touches real media or accounts."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
import threading

import pytest

from lg_sports_deck.core.domain import Conflict
from lg_sports_deck.core.recording import RecorderError
from lg_sports_deck.unified.app import create_app
from lg_sports_deck.unified.sample_retention_test import MARKER, SampleRetentionError
from lg_sports_deck.unified.tests.test_player_alerts import TinyFakeBackend, make_openplay
from lg_sports_deck.unified.tests.test_unified import Clock, ORIGIN, PW, client, login


@pytest.fixture
def isolated(tmp_path):
    app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'new-sample-only-media',{ORIGIN},
                   clock=Clock(),ffmpeg_path='fictional-ffmpeg-not-used',enable_sample_retention=True)
    app.state.owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Owner',PW)
    app.state.unified.set_limits(app.state.owner,4,12)
    app.state.recorder.backend=TinyFakeBackend()
    yield app
    app.state.recorder.close()


def make_clip(app):
    sid,people=make_openplay(app);mid=app.state.engine.assign_next(app.state.owner,sid,1)
    rc=app.state.recorder;rid=rc.start(people[0],mid)['recording_id']
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT count(*) FROM unified_test_sample_retention_managed').fetchone()[0]==0
    assert rc.stop(people[0],mid)['state']=='ready'
    with app.state.engine.db.read() as c:
        row=dict(c.execute('''SELECT r.*,t.expires_at,t.state retention_state FROM recordings r
                             JOIN test_recording_retention t ON t.recording_id=r.id WHERE r.id=?''',(rid,)).fetchone())
    path=app.state.engine.private_recording(people[0],mid)
    return mid,rid,people,path,row


def expiry(row):return datetime.fromisoformat(row['expires_at'])


def test_exact_seven_days_from_ready_and_only_enrolled_file_deleted(isolated):
    app=isolated;mid,rid,people,path,row=make_clip(app);ret=app.state.test_sample_retention
    ready=datetime.fromisoformat(row['ready_at']);end=expiry(row)
    assert end-ready==timedelta(days=7) and row['retention_state']=='scheduled'
    unrelated=path.parent/'unrelated-fictional-note.txt';unrelated.write_text('must survive')
    assert ret.purge_due(end-timedelta(microseconds=1))=={'deleted':0,'failed':0}
    assert path.is_file() and app.state.engine.private_recording(people[1],mid)==path
    assert ret.purge_due(end)=={'deleted':1,'failed':0}
    assert not path.exists() and unrelated.read_text()=='must survive'
    assert ret.purge_due(end+timedelta(days=1))=={'deleted':0,'failed':0}
    with pytest.raises(Conflict):app.state.engine.private_recording(people[0],mid)
    m=app.state.unified.matches(people[0])[0]
    assert m['recording_state']=='expired' and not m['can_watch'] and 'test_file_path' not in m
    with client(app) as c:
        login(c,'p0@example.invalid')
        assert c.get(f'/api/u/matches/{mid}/replay').status_code==409
        assert c.get('/api/u/catalog').json()['sample_auto_retention_test_enabled'] is True


def test_symbolic_link_and_hard_link_are_refused_then_safe_retry_succeeds(isolated,tmp_path):
    app=isolated;mid,rid,people,path,row=make_clip(app);ret=app.state.test_sample_retention
    original=path.read_bytes();path.unlink()
    outside=tmp_path/'do-not-delete.txt';outside.write_text('untouched')
    path.symlink_to(outside)
    assert ret.purge_due(expiry(row))=={'deleted':0,'failed':1}
    assert path.is_symlink() and outside.read_text()=='untouched'
    with pytest.raises(Conflict):app.state.engine.private_recording(people[0],mid)
    path.unlink();os.link(outside,path)
    assert ret.purge_due(expiry(row))=={'deleted':0,'failed':1}
    assert outside.read_text()=='untouched' and path.is_file()
    path.unlink();path.write_bytes(original)
    assert ret.purge_due(expiry(row))=={'deleted':1,'failed':0}
    assert not path.exists() and outside.read_text()=='untouched'


def test_marker_tampering_fails_closed_and_retries_without_touching_files(isolated):
    app=isolated;mid,rid,people,path,row=make_clip(app);ret=app.state.test_sample_retention
    marker=path.parent/MARKER;original=marker.read_text();marker.write_text('not the bound marker')
    assert ret.purge_due(expiry(row))=={'deleted':0,'failed':1} and path.exists()
    with pytest.raises(Conflict):app.state.engine.private_recording(people[0],mid)
    marker.write_text(original)
    assert ret.purge_due(expiry(row))=={'deleted':1,'failed':0} and not path.exists()


def test_expiry_tampering_cannot_shorten_seven_days(isolated):
    app=isolated;mid,rid,people,path,row=make_clip(app);ret=app.state.test_sample_retention
    earlier=expiry(row)-timedelta(days=1)
    with app.state.engine.db.tx() as c:c.execute('UPDATE test_recording_retention SET expires_at=? WHERE recording_id=?',(earlier.isoformat(),rid))
    assert ret.purge_due(expiry(row)+timedelta(days=1))=={'deleted':0,'failed':1} and path.is_file()
    with app.state.engine.db.tx() as c:c.execute('UPDATE test_recording_retention SET expires_at=? WHERE recording_id=?',(row['expires_at'],rid))
    assert ret.purge_due(expiry(row))=={'deleted':1,'failed':0}


def test_unmarked_nonempty_or_symlinked_directories_are_never_adopted(tmp_path):
    d=tmp_path/'old-data';d.mkdir();existing=d/'test_match_000001.mp4';existing.write_bytes(b'old data')
    with pytest.raises(SampleRetentionError):
        create_app(tmp_path/'new.sqlite3',d,{ORIGIN},ffmpeg_path='not-used',enable_sample_retention=True)
    assert existing.read_bytes()==b'old data' and not (d/MARKER).exists()
    target=tmp_path/'target';target.mkdir();link=tmp_path/'link';link.symlink_to(target,target_is_directory=True)
    with pytest.raises(SampleRetentionError):
        create_app(tmp_path/'other.sqlite3',link,{ORIGIN},ffmpeg_path='not-used',enable_sample_retention=True)
    assert list(target.iterdir())==[]


def test_restart_checks_bound_database_and_folder(isolated,tmp_path):
    app=isolated;db=app.state.engine.db.path;folder=app.state.recorder.media_dir
    restart=create_app(db,folder,{ORIGIN},ffmpeg_path='not-used',enable_sample_retention=True)
    assert restart.state.test_sample_retention is not None;restart.state.recorder.close()
    wrong=tmp_path/'another-media'
    with pytest.raises(SampleRetentionError):
        create_app(db,wrong,{ORIGIN},ffmpeg_path='not-used',enable_sample_retention=True)
    with pytest.raises(SampleRetentionError):
        create_app(tmp_path/'other-db.sqlite3',folder,{ORIGIN},ffmpeg_path='not-used',enable_sample_retention=True)


def test_existing_clips_are_never_backfilled_or_deleted(tmp_path):
    db=tmp_path/'fictional.sqlite3';old_dir=tmp_path/'old-media';clock=Clock()
    old=create_app(db,old_dir,{ORIGIN},clock=clock,ffmpeg_path='not-used')
    old.state.owner=old.state.accounts.bootstrap_test_superuser('owner@example.invalid','Owner',PW)
    old.state.unified.set_limits(old.state.owner,4,12);old.state.recorder.backend=TinyFakeBackend()
    sid,people=make_openplay(old);mid=old.state.engine.assign_next(old.state.owner,sid,1)
    old.state.recorder.start(people[0],mid);old.state.recorder.stop(people[0],mid)
    old_file=old.state.engine.private_recording(people[0],mid);old.state.recorder.close()
    new=create_app(db,tmp_path/'separate-empty-sample-media',{ORIGIN},clock=clock,ffmpeg_path='not-used',enable_sample_retention=True)
    try:
        with new.state.engine.db.read() as c:
            assert c.execute('SELECT count(*) FROM unified_test_sample_retention_managed').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM test_recording_retention').fetchone()[0]==0
        assert new.state.test_sample_retention.purge_due(datetime.now(timezone.utc)+timedelta(days=999))=={'deleted':0,'failed':0}
        assert old_file.is_file() and new.state.engine.private_recording(people[0],mid)==old_file
    finally:new.state.recorder.close()


def test_background_loop_runs_during_app_lifespan_on_explicit_test_clock(tmp_path):
    app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'sample-media',{ORIGIN},clock=Clock(),
                   ffmpeg_path='not-used',enable_sample_retention=True,sample_retention_interval=.05,
                   sample_retention_now=lambda:datetime.now(timezone.utc)+timedelta(days=8))
    app.state.owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Owner',PW)
    app.state.unified.set_limits(app.state.owner,4,12);app.state.recorder.backend=TinyFakeBackend()
    mid,rid,people,path,row=make_clip(app)
    ran=threading.Event();original=app.state.test_sample_retention.purge_due
    def measured(now):
        try:return original(now)
        finally:ran.set()
    app.state.test_sample_retention.purge_due=measured
    with client(app) as c:
        assert ran.wait(timeout=5) and not path.exists()
        login(c,'p0@example.invalid')
        assert c.get(f'/api/u/matches/{mid}/replay').status_code==409
    assert ran.is_set()


def test_failed_ready_transaction_never_publishes_an_unscheduled_clip(isolated):
    app=isolated;sid,people=make_openplay(app);eng=app.state.engine;mid=eng.assign_next(app.state.owner,sid,1)
    rid=app.state.recorder.start(people[0],mid)['recording_id']
    marker=app.state.recorder.media_dir/MARKER;marker.write_text('invalid')
    with pytest.raises(RecorderError,match='safely marked ready'):
        app.state.recorder.stop(people[0],mid)
    with eng.db.read() as c:
        assert c.execute('SELECT state FROM recordings WHERE id=?',(rid,)).fetchone()[0]=='failed'
        assert c.execute('SELECT count(*) FROM unified_test_sample_retention_managed').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM test_recording_retention').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM outbox WHERE kind='replay'").fetchone()[0]==0
    with pytest.raises(Conflict):eng.private_recording(people[0],mid)


def test_expired_sample_is_denied_even_when_background_worker_is_off(isolated):
    app=isolated;mid,rid,people,path,row=make_clip(app)
    ready=datetime.now(timezone.utc)-timedelta(days=8);expired=ready+timedelta(days=7)
    with app.state.engine.db.tx() as c:
        c.execute('UPDATE recordings SET ready_at=? WHERE id=?',(ready.isoformat(timespec='microseconds'),rid))
        c.execute('UPDATE test_recording_retention SET expires_at=? WHERE recording_id=?',(expired.isoformat(timespec='microseconds'),rid))
    assert path.is_file()  # worker intentionally has not run
    with pytest.raises(Conflict):app.state.engine.private_recording(people[0],mid)
    m=app.state.unified.matches(people[0])[0]
    assert m['recording_state']=='expired' and not m['can_watch'] and path.exists()


def test_interrupted_unlink_recovers_idempotently(isolated):
    app=isolated;mid,rid,people,path,row=make_clip(app)
    with app.state.engine.db.tx() as c:c.execute("UPDATE test_recording_retention SET state='deleting' WHERE recording_id=?",(rid,))
    path.unlink()  # Simulate an already completed unlink before a crash.
    assert app.state.test_sample_retention.purge_due(expiry(row))=={'deleted':1,'failed':0}
    assert app.state.test_sample_retention.purge_due(expiry(row))=={'deleted':0,'failed':0}
    with app.state.engine.db.read() as c:
        assert c.execute('SELECT state FROM test_recording_retention WHERE recording_id=?',(rid,)).fetchone()[0]=='deleted'


def test_wrong_database_path_never_deletes_external_file(isolated,tmp_path):
    app=isolated;mid,rid,people,path,row=make_clip(app)
    outside=tmp_path/'do-not-delete.mp4';outside.write_bytes(b'external file')
    with app.state.engine.db.tx() as c:c.execute('UPDATE recordings SET file_path=? WHERE id=?',(str(outside),rid))
    assert app.state.test_sample_retention.purge_due(expiry(row))=={'deleted':0,'failed':1}
    assert outside.read_bytes()==b'external file' and path.exists()


def test_actual_stock_ffmpeg_records_and_enrolls_seven_day_cleanup(tmp_path):
    ffmpeg=os.environ.get('FFMPEG_TEST_BIN')
    if not ffmpeg:pytest.skip('FFMPEG_TEST_BIN required for sample recording test')
    import subprocess
    import time
    app=create_app(tmp_path/'fictional.sqlite3',tmp_path/'new-sample-media',{ORIGIN},
                   clock=Clock(),ffmpeg_path=ffmpeg,enable_sample_retention=True)
    app.state.owner=app.state.accounts.bootstrap_test_superuser('owner@example.invalid','Owner',PW)
    app.state.unified.set_limits(app.state.owner,4,12)
    try:
        sid,people=make_openplay(app);mid=app.state.engine.assign_next(app.state.owner,sid,1)
        rid=app.state.recorder.start(people[0],mid)['recording_id'];time.sleep(1.5)
        assert app.state.recorder.stop(people[0],mid)['state']=='ready'
        path=app.state.engine.private_recording(people[0],mid)
        subprocess.run([ffmpeg,'-v','error','-i',str(path),'-f','null','-'],check=True,capture_output=True,timeout=20)
        with app.state.engine.db.read() as c:
            row=dict(c.execute('SELECT r.ready_at,t.expires_at FROM recordings r JOIN test_recording_retention t ON t.recording_id=r.id WHERE r.id=?',(rid,)).fetchone())
        assert expiry(row)-datetime.fromisoformat(row['ready_at'])==timedelta(days=7)
        assert app.state.test_sample_retention.purge_due(expiry(row))=={'deleted':1,'failed':0} and not path.exists()
    finally:app.state.recorder.close()
