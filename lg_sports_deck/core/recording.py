"""Internal per-court recording coordinator with synthetic video only.

No RTSP, live camera, network control, real email, or production deployment.
The coordinator is deliberately single-process. Database rows do not claim a
recording is ready unless the synthetic backend finalizes a valid test file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import threading
import time
from typing import Protocol

from .domain import Conflict, Engine, PermissionDenied, timestamp


class RecorderError(Exception):
    pass


class Backend(Protocol):
    def start(self, court_id: int, match_id: int, destination: Path): ...
    def stop(self, handle) -> Path: ...
    def abort(self, handle) -> None: ...


@dataclass
class SyntheticHandle:
    proc: subprocess.Popen
    partial: Path
    destination: Path


class SyntheticFFmpegBackend:
    """Write a test pattern or explicitly labelled stock sample, never a camera.

    The optional sample is a local file chosen by the developer, not user media.
    It loops in real time while this fictional recorder is running; stopping
    creates a separate test file for that match. It is not actual match footage.
    """
    def __init__(self, executable: str, *, sample_video: str | Path | None = None):
        self.executable=executable
        self.sample_video=Path(sample_video).resolve() if sample_video is not None else None
        if self.sample_video is not None and not self.sample_video.is_file():
            raise ValueError('The local fictional stock sample is unavailable.')

    def start(self, court_id: int, match_id: int, destination: Path) -> SyntheticHandle:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial=destination.with_name(destination.stem+'.partial.mp4')
        source=(['-stream_loop','-1','-i',str(self.sample_video),'-an'] if self.sample_video
                else ['-f','lavfi','-i','testsrc2=size=320x180:rate=10'])
        encode=(['-c:v','libx264','-preset','veryfast','-crf','27'] if self.sample_video
                else ['-c:v','libx264','-preset','ultrafast'])
        cmd=[self.executable,'-hide_banner','-loglevel','error','-nostats','-re',*source,*encode,
             '-pix_fmt','yuv420p','-movflags','+faststart','-y',str(partial)]
        try:
            proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RecorderError('Cannot start synthetic test recorder.') from exc
        time.sleep(0.2)  # Allow an immediately failing process to surface in internal tests.
        if proc.poll() is not None:
            raise RecorderError('Synthetic test recorder exited during startup.')
        return SyntheticHandle(proc,partial,destination)

    def stop(self, handle: SyntheticHandle) -> Path:
        proc=handle.proc
        try:
            if proc.poll() is None:
                if proc.stdin is None: raise RecorderError('Test recorder control pipe missing.')
                proc.stdin.write(b'q');proc.stdin.flush()
            code=proc.wait(timeout=8)
            if code!=0 or not handle.partial.is_file() or handle.partial.stat().st_size<512:
                raise RecorderError('Synthetic test clip did not finalize.')
            probe=subprocess.run([self.executable,'-hide_banner','-v','error','-i',str(handle.partial),'-f','null','-'],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            if probe.returncode!=0: raise RecorderError('Synthetic test clip could not be decoded.')
            os.replace(handle.partial,handle.destination)
            return handle.destination
        except (OSError,subprocess.TimeoutExpired) as exc:
            self.abort(handle)
            raise RecorderError('Synthetic test recorder failed to stop.') from exc
        finally:
            if proc.stdin is not None:
                try: proc.stdin.close()
                except OSError: pass

    def abort(self, handle: SyntheticHandle) -> None:
        proc=handle.proc
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait(timeout=5)
        if proc.stdin is not None:
            try: proc.stdin.close()
            except OSError: pass


class RecordingCoordinator:
    """Serializes test recorder transitions per court within one process.

    Real deployment needs an authenticated on-site agent, durable commands,
    multi-process locking/recovery, streaming, storage and privacy review.
    """
    def __init__(self, engine: Engine, backend: Backend, media_dir: str | Path):
        self.engine=engine
        self.backend=backend
        self.media_dir=Path(media_dir).resolve()
        self.media_dir.mkdir(parents=True,exist_ok=True)
        self._locks={1:threading.RLock(),2:threading.RLock()}
        self._handles: dict[int,tuple[int,object]]={}
        # Optional fictional-only transactional hook. None in the core and
        # older demos; the unified sample-retention lab may opt in explicitly.
        self.test_ready_hook=None

    def _authorized(self, c, actor: int, match_id: int) -> None:
        user=Engine._user(c,actor)
        participant=c.execute('SELECT 1 FROM match_players WHERE match_id=? AND user_id=?',(match_id,actor)).fetchone()
        if user['role']!='superuser' and not (user['role']=='player' and participant):
            raise PermissionDenied('Only a match participant or Superuser may control this test recording.')

    def start(self, actor: int, match_id: int) -> dict:
        with self.engine.db.read() as c:
            m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not m: raise ValueError('Match not found.')
            self._authorized(c,actor,match_id)
            court=m['court_id']
        with self._locks[court]:
            with self.engine.db.read() as c:
                m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
                self._authorized(c,actor,match_id)
                if m['status']!='active': raise Conflict('Only a current match can start recording.')
                existing=c.execute('SELECT * FROM recordings WHERE match_id=?',(match_id,)).fetchone()
                if existing:
                    if existing['state']=='recording' and court in self._handles and self._handles[court][0]==existing['id']:
                        return {'recording_id':existing['id'],'state':'recording','already_running':True,'previous_failed':False}
                    raise Conflict('This match already has a recording or failed attempt; recovery rules are not finalized.')
                # An interrupted recorder may still have a live process after a
                # failed abort or crash. Stay fail-closed until investigated;
                # don't silently start a second recorder on that court.
                prior=c.execute("SELECT * FROM recordings WHERE court_id=? AND state IN ('starting','recording','stopping','interrupted') ORDER BY id DESC LIMIT 1",(court,)).fetchone()
            previous_failed=False
            if prior:
                owned=self._handles.get(court)
                if not owned or owned[0]!=prior['id']:
                    raise Conflict('Prior recorder ownership is unknown. Stop and investigate before starting another recording.')
                with self.engine.db.read() as c:
                    prior_match=c.execute('SELECT status FROM matches WHERE id=?',(prior['match_id'],)).fetchone()
                    if not prior_match or prior_match['status']!='ended':
                        raise Conflict('The earlier match on this court has not ended.')
                previous_failed=not self._finish_locked(court,prior['id'])
            with self.engine.db.tx() as c:
                m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
                self._authorized(c,actor,match_id)
                if m['status']!='active': raise Conflict('The new match is no longer active.')
                cur=c.execute("INSERT INTO recordings(match_id,court_id,state,source,started_at) VALUES(?,?,'starting','synthetic_test',?)",(match_id,court,timestamp()))
                rec_id=int(cur.lastrowid)
            dest=self.media_dir/f'test_match_{match_id:06d}.mp4'
            try:
                handle=self.backend.start(court,match_id,dest)
            except Exception as exc:
                with self.engine.db.tx() as c:
                    c.execute("UPDATE recordings SET state='failed',failure='Synthetic recorder failed to start' WHERE id=?",(rec_id,))
                raise RecorderError('Synthetic recorder failed to start; no ready clip was created.') from exc
            self._handles[court]=(rec_id,handle)
            with self.engine.db.tx() as c:
                c.execute("UPDATE recordings SET state='recording' WHERE id=?",(rec_id,))
            return {'recording_id':rec_id,'state':'recording','already_running':False,'previous_failed':previous_failed}

    def _finish_locked(self, court: int, rec_id: int) -> bool:
        owned=self._handles.get(court)
        if not owned or owned[0]!=rec_id:
            raise Conflict('Recorder ownership is unknown; cannot mark it ready.')
        handle=owned[1]
        with self.engine.db.tx() as c:
            c.execute("UPDATE recordings SET state='stopping' WHERE id=? AND state='recording'",(rec_id,))
        try:
            file_path=self.backend.stop(handle)
            file_path=Path(file_path).resolve()
            if not file_path.is_relative_to(self.media_dir) or not file_path.is_file() or file_path.stat().st_size==0:
                raise RecorderError('Synthetic backend returned no valid private file.')
        except Exception:
            try:
                self.backend.abort(handle)
            except Exception as exc:
                # Unknown process state means we cannot safely acquire the court for a new recording.
                with self.engine.db.tx() as c:
                    c.execute("UPDATE recordings SET state='interrupted',failure='Unable to stop synthetic recorder' WHERE id=?",(rec_id,))
                self._handles.pop(court,None)
                raise Conflict('Synthetic recorder status is unknown; manual recovery required.') from exc
            with self.engine.db.tx() as c:
                c.execute("UPDATE recordings SET state='failed',failure='Synthetic clip could not be finalized' WHERE id=?",(rec_id,))
            self._handles.pop(court,None)
            return False
        self._handles.pop(court,None)
        try:
            with self.engine.db.tx() as c:
                r=c.execute('SELECT * FROM recordings WHERE id=?',(rec_id,)).fetchone()
                ready_at=timestamp()
                c.execute("UPDATE recordings SET state='ready',file_path=?,ready_at=? WHERE id=?",(str(file_path),ready_at,rec_id))
                if self.test_ready_hook is not None:
                    self.test_ready_hook(c,r,file_path,ready_at)
                Engine._enqueue(c,r['match_id'],'replay')
        except Exception as exc:
            # A finalized file alone is not a ready recording. Keep failures
            # unavailable and visible; orphaned test files need investigation.
            with self.engine.db.tx() as c:
                c.execute("UPDATE recordings SET state='failed',failure='Synthetic ready transaction failed' WHERE id=?",(rec_id,))
            raise RecorderError('Synthetic clip could not be safely marked ready.') from exc
        return True

    def stop(self, actor: int, match_id: int) -> dict:
        with self.engine.db.read() as c:
            m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not m: raise ValueError('Match not found.')
            self._authorized(c,actor,match_id)
            court=m['court_id']
        with self._locks[court]:
            with self.engine.db.read() as c:
                r=c.execute('SELECT * FROM recordings WHERE match_id=?',(match_id,)).fetchone()
                if not r: raise Conflict('No test recording exists for this match.')
                if r['state']=='ready': return {'recording_id':r['id'],'state':'ready','already_stopped':True}
                if r['state']!='recording': raise Conflict('Recording is not in a stoppable test state.')
            ok=self._finish_locked(court,r['id'])
            return {'recording_id':r['id'],'state':'ready' if ok else 'failed','already_stopped':False}

    def close(self) -> None:
        """Stop lingering synthetic processes after a test, without fabricating success."""
        for court in (1,2):
            with self._locks[court]:
                owned=self._handles.pop(court,None)
                if not owned: continue
                rec_id,handle=owned
                try: self.backend.abort(handle)
                finally:
                    with self.engine.db.tx() as c:
                        c.execute("UPDATE recordings SET state='interrupted',failure='Synthetic test ended before finalization' WHERE id=? AND state IN ('starting','recording','stopping')",(rec_id,))
