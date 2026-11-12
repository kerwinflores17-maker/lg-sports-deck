"""Seven-day lifecycle primitives for synthetic test files only.

Nothing in this core module is scheduled automatically. The user selected
video-ready time as the seven-day anchor, but backups, storage architecture
and real deletion still require review. This module accepts an explicit
*fictional test anchor*; it is not a retention
policy for an actual venue and cannot safely delete real recordings.
"""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
from pathlib import Path
import threading

from .domain import Conflict, Engine, timestamp


class TestRetention:
    __test__=False
    def __init__(self, engine: Engine, test_media_dir: str | Path):
        self.engine=engine
        self.test_media_dir=Path(test_media_dir).resolve()
        self._lock=threading.RLock()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('An explicit timezone-aware fictional test time is required.')
        return value.astimezone(timezone.utc)

    def set_fictional_anchor(self, recording_id: int, anchor: datetime) -> datetime:
        """Use an explicit sample time; do not infer the venue's real trigger."""
        expires=self._utc(anchor)+timedelta(days=7)
        with self._lock,self.engine.db.tx() as c:
            rec=c.execute('SELECT * FROM recordings WHERE id=?',(recording_id,)).fetchone()
            if not rec or rec['source']!='synthetic_test' or rec['state']!='ready' or not rec['file_path']:
                raise Conflict('Only a finalized synthetic test clip may be scheduled.')
            if c.execute('SELECT 1 FROM test_recording_retention WHERE recording_id=?',(recording_id,)).fetchone():
                raise Conflict('A test retention schedule already exists.')
            c.execute("INSERT INTO test_recording_retention(recording_id,expires_at,state) VALUES(?,?,'scheduled')",
                      (recording_id,expires.isoformat(timespec='microseconds')))
        return expires

    def purge_due_test_clips(self, as_of: datetime) -> dict:
        """Explicitly called by tests; no timer or production worker is installed.

        Deny replay access as soon as the expiry has passed even if a deletion
        attempt later fails. Refuse anything outside the configured test folder
        or not bearing the generated synthetic filename.
        """
        now=self._utc(as_of);result={'deleted':0,'failed':0}
        with self._lock:
            with self.engine.db.read() as c:
                rows=list(c.execute('''SELECT t.recording_id,t.expires_at,t.state,r.match_id,r.file_path,r.source
                    FROM test_recording_retention t JOIN recordings r ON r.id=t.recording_id
                    WHERE t.state IN ('scheduled','deleting','delete_failed')'''))
            for r in rows:
                expiry=self._utc(datetime.fromisoformat(r['expires_at']))
                if now<expiry:continue
                with self.engine.db.tx() as c:
                    c.execute("UPDATE test_recording_retention SET state='deleting',failure=NULL WHERE recording_id=?",(r['recording_id'],))
                try:
                    if r['source']!='synthetic_test' or not r['file_path']:
                        raise ValueError('Unexpected recording source or path.')
                    path=Path(r['file_path'])
                    expected=self.test_media_dir/f'test_match_{r["match_id"]:06d}.mp4'
                    if path!=expected or path.is_symlink() or not path.resolve(strict=False).is_relative_to(self.test_media_dir):
                        raise ValueError('Refusing a non-test path.')
                    path.unlink(missing_ok=True)
                except (OSError,ValueError):
                    with self.engine.db.tx() as c:
                        c.execute("UPDATE test_recording_retention SET state='delete_failed',failure='Synthetic file deletion failed safely' WHERE recording_id=?",(r['recording_id'],))
                    result['failed']+=1
                    continue
                with self.engine.db.tx() as c:
                    c.execute("UPDATE test_recording_retention SET state='deleted',deleted_at=?,failure=NULL WHERE recording_id=?",(timestamp(),r['recording_id']))
                    c.execute('UPDATE recordings SET file_path=NULL WHERE id=?',(r['recording_id'],))
                result['deleted']+=1
        return result
