"""Test-only bridge from the fictional booking demo to the domain prototype.

This is NOT a production integration or payment webhook. It reads simulated
booking records from one demo sandbox and creates corresponding .invalid test
users/paid registrations in a separate internal database. It is never mounted
as a network endpoint. A real implementation needs authenticated identities,
transactional events, cancellations/no-shows and provider reconciliation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from pathlib import Path

from .domain import Engine, PermissionDenied, Conflict


class StagingBridge:
    def __init__(self, staging_db: str | Path, engine: Engine, superuser_id: int, sandbox_id: str):
        self.stage=Path(staging_db).resolve()
        if not self.stage.is_file() or not re.fullmatch(r'[a-f0-9]{48}',sandbox_id):
            raise ValueError('Invalid fictional staging sandbox.')
        self.engine=engine;self.superuser_id=superuser_id;self.sandbox_id=sandbox_id
        self._lock=threading.RLock()
        with self._stage() as c:
            if not c.execute('SELECT 1 FROM sandboxes WHERE id=?',(sandbox_id,)).fetchone():
                raise ValueError('Fictional sandbox not found.')
        with self.engine.db.tx() as c:
            if self.engine._user(c,superuser_id)['role']!='superuser':
                raise PermissionDenied('A fictional Superuser is required for internal fixture mapping.')
            c.execute("CREATE TABLE IF NOT EXISTS bridge_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), sandbox_id TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS bridge_users (stage_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id))")
            c.execute("CREATE TABLE IF NOT EXISTS bridge_sessions (stage_id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL UNIQUE REFERENCES openplay_sessions(id))")
            row=c.execute('SELECT sandbox_id FROM bridge_meta WHERE singleton=1').fetchone()
            if row and row['sandbox_id']!=sandbox_id:
                raise Conflict('This internal domain database belongs to another test sandbox.')
            c.execute('INSERT OR IGNORE INTO bridge_meta(singleton,sandbox_id) VALUES(1,?)',(sandbox_id,))

    @contextmanager
    def _stage(self):
        con=sqlite3.connect(str(self.stage),timeout=15)
        con.row_factory=sqlite3.Row
        try:yield con
        finally:con.close()

    def register_session(self, staging_session_id: int, test_venue_token: str) -> int:
        """Map one session to a fictional token. Nothing is printed or scanned."""
        if not test_venue_token: raise ValueError('A fictional venue token is required.')
        with self._lock:
            with self._stage() as s:
                row=s.execute('SELECT * FROM sessions WHERE id=? AND sandbox_id=?',(staging_session_id,self.sandbox_id)).fetchone()
                if not row: raise PermissionDenied('Session is not in this fictional sandbox.')
            with self.engine.db.read() as c:
                existing=c.execute('''SELECT b.session_id,o.qr_token_hash FROM bridge_sessions b
                                      JOIN openplay_sessions o ON o.id=b.session_id WHERE b.stage_id=?''',(staging_session_id,)).fetchone()
                if existing:
                    digest=hashlib.sha256(test_venue_token.encode()).hexdigest()
                    if not hmac.compare_digest(existing['qr_token_hash'],digest):
                        raise Conflict('A different fictional token was already registered.')
                    return int(existing['session_id'])
            domain_id=self.engine.create_session(self.superuser_id,f'Test-only: {row["label"]}',test_venue_token,int(row['capacity']))
            with self.engine.db.tx() as c:
                c.execute('INSERT INTO bridge_sessions(stage_id,session_id) VALUES(?,?)',(staging_session_id,domain_id))
            return domain_id

    def _map_player(self, staging_profile_id: int) -> int:
        with self.engine.db.read() as c:
            m=c.execute('SELECT user_id FROM bridge_users WHERE stage_id=?',(staging_profile_id,)).fetchone()
            if m:return int(m['user_id'])
        with self._stage() as c:
            p=c.execute('SELECT id,name FROM profiles WHERE id=? AND sandbox_id=?',(staging_profile_id,self.sandbox_id)).fetchone()
            if not p:raise PermissionDenied('Player is not in this fictional sandbox.')
        address=f'sandbox-{self.sandbox_id[:12]}-profile-{staging_profile_id}@example.invalid'
        user_id=self.engine.create_fixture_user(address,p['name'],secrets.token_urlsafe(24),'player')
        with self.engine.db.tx() as c:
            c.execute('INSERT INTO bridge_users(stage_id,user_id) VALUES(?,?)',(staging_profile_id,user_id))
        return user_id

    def check_in(self, staging_session_id: int, staging_profile_id: int, test_venue_token: str) -> dict:
        """Bridge only a participant in a confirmed *simulated* Open Play booking."""
        with self._lock:
            with self._stage() as c:
                paid_fixture=c.execute('''SELECT 1 FROM bookings b
                    JOIN booking_players bp ON bp.booking_id=b.id
                    JOIN profiles p ON p.id=bp.player_id AND p.sandbox_id=b.sandbox_id
                    WHERE b.sandbox_id=? AND b.kind='openplay' AND b.session_id=? AND bp.player_id=?''',
                    (self.sandbox_id,staging_session_id,staging_profile_id)).fetchone()
                if not paid_fixture: raise PermissionDenied('A confirmed simulated booking is required.')
            with self.engine.db.read() as c:
                session=c.execute('''SELECT b.session_id,o.qr_token_hash FROM bridge_sessions b
                    JOIN openplay_sessions o ON o.id=b.session_id WHERE b.stage_id=?''',(staging_session_id,)).fetchone()
                if not session: raise Conflict('Map the fictional session before check-in.')
                digest=hashlib.sha256(test_venue_token.encode()).hexdigest()
                if not hmac.compare_digest(session['qr_token_hash'],digest):
                    raise PermissionDenied('Invalid fictional venue token.')
            player_id=self._map_player(staging_profile_id);domain_session=int(session['session_id'])
            with self.engine.db.read() as c:
                registered=c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(domain_session,player_id)).fetchone()
            if not registered:self.engine.fixture_test_payment(self.superuser_id,domain_session,player_id)
            checkin_id=self.engine.check_in(domain_session,player_id,test_venue_token)
            return {'checkin_id':checkin_id,'session_id':domain_session,'user_id':player_id,'mode':'simulated-only'}
