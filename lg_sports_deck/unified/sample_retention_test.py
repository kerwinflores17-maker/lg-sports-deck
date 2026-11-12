"""Opt-in automatic retention LAB for newly created fictional sample clips only.

NEVER use this module with real cameras, personal data or real venue recordings.
Requires a new, empty, non-symlinked media directory, bound to one fictional
SQLite database by an ownership marker. It enrolls *new* unified synthetic
recordings atomically at ready time; no existing clip is backfilled or deleted.
A single-process app lifespan can run a periodic sweep, but this is NOT a
production scheduler. Backups, exported copies, cloud, NVR and restart monitoring
are out of scope; a test success does not guarantee real seven-day deletion.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
from typing import Callable

from lg_sports_deck.core.domain import Engine


MARKER='.lg-fictional-sample-retention-owner'
PREFIX='LGSD-TEST-SAMPLE-ONLY:'
INTERVAL_SECONDS=60


class SampleRetentionError(Exception):
    """Configuration or file refused; no outside data should be removed."""


class SampleAutoRetention:
    """A scoped local cleanup experiment, not a real-media deletion service."""
    __test__=False

    def __init__(self,engine: Engine,media_dir: str|Path):
        self.engine=engine
        self._lock=threading.RLock()
        self.last_sweep_status='not_run'
        raw=Path(os.path.abspath(os.path.expanduser(str(media_dir))))
        if raw.is_symlink() or not raw.is_dir() or raw.resolve()!=raw:
            raise SampleRetentionError('Use a real, non-symlinked, dedicated empty test media directory.')
        self.media_dir=raw
        self._install_schema()
        self._initialize_or_validate_owner()

    def _install_schema(self):
        with self.engine.db.read() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS unified_test_sample_retention_owner (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              media_path TEXT NOT NULL,
              marker_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS unified_test_sample_retention_managed (
              recording_id INTEGER PRIMARY KEY REFERENCES recordings(id),
              marker_sha256 TEXT NOT NULL
            );
            ''')

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('An explicit timezone-aware test time is required.')
        return value.astimezone(timezone.utc)

    def _read_marker(self) -> str:
        marker=self.media_dir/MARKER
        try:
            fd=os.open(marker,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:
                st=os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>256:
                    raise SampleRetentionError('Fictional sample owner marker is invalid.')
                content=os.read(fd,257).decode('ascii')
            finally:os.close(fd)
        except (OSError,UnicodeError) as exc:
            raise SampleRetentionError('Fictional sample owner marker is unavailable.') from exc
        if not content.startswith(PREFIX) or len(content)<len(PREFIX)+32:
            raise SampleRetentionError('Fictional sample owner marker is invalid.')
        return hashlib.sha256(content.encode('ascii')).hexdigest()

    def _initialize_or_validate_owner(self):
        marker=self.media_dir/MARKER
        created=False
        try:
            with self.engine.db.tx() as c:
                row=c.execute('SELECT * FROM unified_test_sample_retention_owner WHERE singleton=1').fetchone()
                if row is None:
                    # Crucial safety boundary: never adopt a folder containing
                    # old media, databases, user files, or another marker.
                    if next(self.media_dir.iterdir(),None) is not None:
                        raise SampleRetentionError('An empty dedicated test media directory is required; no files were removed.')
                    token=PREFIX+secrets.token_urlsafe(32)
                    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
                    created=True
                    try:os.write(fd,token.encode('ascii'))
                    finally:os.close(fd)
                    digest=hashlib.sha256(token.encode('ascii')).hexdigest()
                    c.execute('INSERT INTO unified_test_sample_retention_owner VALUES(1,?,?)',(str(self.media_dir),digest))
                    self.marker_sha256=digest
                else:
                    if row['media_path']!=str(self.media_dir):
                        raise SampleRetentionError('This test database belongs to a different sample directory.')
                    actual=self._read_marker()
                    if not hmac.compare_digest(actual,row['marker_sha256']):
                        raise SampleRetentionError('Fictional sample ownership does not match this test database.')
                    self.marker_sha256=actual
        except Exception:
            if created:
                try:marker.unlink(missing_ok=True)
                except OSError:pass
            raise

    def _assert_owner(self):
        if self.media_dir.is_symlink() or not self.media_dir.is_dir() or self.media_dir.resolve()!=self.media_dir:
            raise SampleRetentionError('Fictional sample directory changed.')
        actual=self._read_marker()
        if not hmac.compare_digest(actual,self.marker_sha256):
            raise SampleRetentionError('Fictional sample ownership changed.')
        with self.engine.db.read() as c:
            row=c.execute('SELECT media_path,marker_sha256 FROM unified_test_sample_retention_owner WHERE singleton=1').fetchone()
        if not row or row['media_path']!=str(self.media_dir) or not hmac.compare_digest(row['marker_sha256'],actual):
            raise SampleRetentionError('Fictional sample database ownership changed.')

    def schedule_in_ready_transaction(self,c: sqlite3.Connection,rec: sqlite3.Row,path: Path,ready_at: str):
        """Enroll exactly this new fictional clip, alongside the ready update."""
        self._assert_owner()
        expected=self.media_dir/f'test_match_{rec["match_id"]:06d}.mp4'
        if rec['source']!='synthetic_test' or path!=expected or path.is_symlink():
            raise SampleRetentionError('Only a newly finalized local fictional sample is eligible.')
        try:st=path.stat(follow_symlinks=False)
        except OSError as exc:raise SampleRetentionError('Fictional sample is unavailable.') from exc
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
            raise SampleRetentionError('Only an independent regular sample file is eligible.')
        try:ready=self._utc(datetime.fromisoformat(ready_at))
        except (TypeError,ValueError) as exc:raise SampleRetentionError('Invalid fictional ready time.') from exc
        expiry=(ready+timedelta(days=7)).isoformat(timespec='microseconds')
        c.execute("INSERT INTO test_recording_retention(recording_id,expires_at,state) VALUES(?,?,'scheduled')",(rec['id'],expiry))
        c.execute('INSERT INTO unified_test_sample_retention_managed(recording_id,marker_sha256) VALUES(?,?)',
                  (rec['id'],self.marker_sha256))

    def _unlink_scoped_file(self,path: Path,match_id: int):
        self._assert_owner()
        expected=self.media_dir/f'test_match_{match_id:06d}.mp4'
        if path!=expected or path.parent!=self.media_dir:
            raise SampleRetentionError('Refusing a non-test sample path.')
        # Directory-relative operations avoid following a final-component link.
        fd=os.open(self.media_dir,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            try:st=os.stat(expected.name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:return  # already absent after a prior interrupted unlink
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
                raise SampleRetentionError('Refusing a linked or non-regular sample file.')
            os.unlink(expected.name,dir_fd=fd)
        finally:os.close(fd)

    def purge_due(self,as_of: datetime) -> dict:
        """Delete only enrolled sample files at/after exact ready+7 days.

        Mark unavailable before unlinking. A failed unlink stays unavailable and
        is retried on a later sweep; interrupted unlinks are idempotent. This
        single-process lab does not delete backups or guarantee uptime.
        """
        now=self._utc(as_of);result={'deleted':0,'failed':0}
        with self._lock:
            with self.engine.db.read() as c:
                rows=list(c.execute('''SELECT r.id,r.match_id,r.source,r.state AS recording_state,r.file_path,r.ready_at,
                    t.expires_at,t.state AS retention_state,m.marker_sha256
                    FROM unified_test_sample_retention_managed m
                    JOIN recordings r ON r.id=m.recording_id
                    JOIN test_recording_retention t ON t.recording_id=r.id
                    WHERE t.state IN ('scheduled','deleting','delete_failed')'''))
            for row in rows:
                try:
                    ready=self._utc(datetime.fromisoformat(row['ready_at']))
                    expiry=self._utc(datetime.fromisoformat(row['expires_at']))
                    if expiry!=ready+timedelta(days=7):
                        raise SampleRetentionError('Fictional sample expiry does not match ready time.')
                    if now<expiry:continue
                    if (row['source']!='synthetic_test' or row['recording_state']!='ready' or not row['file_path']
                        or not hmac.compare_digest(row['marker_sha256'],self.marker_sha256)):
                        raise SampleRetentionError('Fictional sample enrollment is invalid.')
                    self._assert_owner()
                    with self.engine.db.tx() as c:
                        changed=c.execute('''UPDATE test_recording_retention SET state='deleting',failure=NULL
                           WHERE recording_id=? AND state IN ('scheduled','deleting','delete_failed') AND expires_at=?''',
                           (row['id'],row['expires_at'])).rowcount
                    if not changed:continue
                    self._unlink_scoped_file(Path(row['file_path']),int(row['match_id']))
                    with self.engine.db.tx() as c:
                        c.execute("UPDATE test_recording_retention SET state='deleted',deleted_at=?,failure=NULL WHERE recording_id=? AND state='deleting'",
                                  (now.isoformat(timespec='microseconds'),row['id']))
                        c.execute('UPDATE recordings SET file_path=NULL WHERE id=?',(row['id'],))
                    result['deleted']+=1
                except (OSError,ValueError,TypeError,SampleRetentionError):
                    with self.engine.db.tx() as c:
                        c.execute("UPDATE test_recording_retention SET state='delete_failed',failure='Sample deletion failed safely' WHERE recording_id=? AND state<>'deleted'",(row['id'],))
                    result['failed']+=1
        return result

    async def run_periodically(self,stop: asyncio.Event,*,interval: float=INTERVAL_SECONDS,
                             now: Callable[[],datetime]=lambda:datetime.now(timezone.utc)):
        """A lightweight opt-in test loop; not a durable production scheduler."""
        if not 0.05<=interval<=3600:raise ValueError('Invalid fictional sample sweep interval.')
        while not stop.is_set():
            try:
                result=await asyncio.to_thread(self.purge_due,now())
                self.last_sweep_status='failed' if result['failed'] else 'ok'
            except Exception:
                # Fixed, non-sensitive health code; retry on the next cycle.
                # Durable operator monitoring remains a production gap.
                self.last_sweep_status='failed'
            try:await asyncio.wait_for(stop.wait(),timeout=interval)
            except asyncio.TimeoutError:pass
