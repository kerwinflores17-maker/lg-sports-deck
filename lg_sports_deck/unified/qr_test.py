"""Rotating QR check-in for the fictional unified integration test only.

This module creates real QR images, but authorizes only .invalid test accounts
and simulated registrations. It does NOT establish physical presence: a QR can
be photographed or shared during its short lifetime. Live check-in windows,
location/anti-abuse controls, production identity and policy are not finalized.
No QR image or raw challenge is saved on disk or logged by this module.
"""
from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timezone
import hashlib
import hmac
import io
import re
import secrets

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from lg_sports_deck.core.domain import Engine, PermissionDenied
from .domain import UnifiedDemo


QR_LIFETIME_SECONDS = 180
QR_PREFIX = 'LGSDTEST1'
CHALLENGE_RE = re.compile(r'^[A-Za-z0-9_-]{32}$', re.ASCII)


class FictionalQR:
    """Issue a short-lived shared test QR and atomically check in a paid player."""
    def __init__(self, demo: UnifiedDemo):
        self.demo = demo
        self.db = demo.db
        with self.db.read() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS unified_test_qr_challenges (
              session_id INTEGER PRIMARY KEY REFERENCES unified_sessions(session_id),
              challenge_hash TEXT NOT NULL CHECK(length(challenge_hash)=64),
              issued_by INTEGER NOT NULL REFERENCES users(id),
              issued_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL CHECK(expires_at>issued_at)
            );
            ''')

    @staticmethod
    def _png_data_url(payload: str) -> str:
        image = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=4)
        image.add_data(payload)
        image.make(fit=True)
        png = image.make_image(fill_color='black', back_color='white')
        stream = io.BytesIO()
        png.save(stream, format='PNG')
        return 'data:image/png;base64,' + b64encode(stream.getvalue()).decode('ascii')

    def issue(self, actor: int, session_id: int) -> dict:
        """Only Superuser or explicitly granted staff can issue/rotate a code.

        One challenge may be used by several registered test players until it
        expires. Issuing a replacement revokes the prior challenge immediately.
        """
        if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id < 1:
            raise ValueError('Choose a fictional test session.')
        # Reject unprivileged or unknown-session requests before the more
        # expensive image work, then recheck both inside the write transaction.
        with self.db.read() as c:
            self.demo._require(c, actor, 'attendance.qr.display')
            if not c.execute('SELECT 1 FROM unified_sessions WHERE session_id=?', (session_id,)).fetchone():
                raise ValueError('Fictional unified session not found.')
        challenge = secrets.token_urlsafe(24)
        if not CHALLENGE_RE.fullmatch(challenge):  # invariant, fail closed
            raise RuntimeError('Test QR challenge generation failed.')
        payload = f'{QR_PREFIX}:{session_id}:{challenge}'
        image = self._png_data_url(payload)
        digest = hashlib.sha256(challenge.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            self.demo._require(c, actor, 'attendance.qr.display')
            if not c.execute('SELECT 1 FROM unified_sessions WHERE session_id=?', (session_id,)).fetchone():
                raise ValueError('Fictional unified session not found.')
            now = int(self.demo.clock())
            expires = now + QR_LIFETIME_SECONDS
            c.execute('''INSERT INTO unified_test_qr_challenges
                         (session_id,challenge_hash,issued_by,issued_at,expires_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                         challenge_hash=excluded.challenge_hash,issued_by=excluded.issued_by,
                         issued_at=excluded.issued_at,expires_at=excluded.expires_at''',
                      (session_id, digest, actor, now, expires))
            self.demo._audit(c, actor, 'unified.test_qr_issued', session_id)
        # The challenge exists in the image and response only, not DB/audit.
        return {'session_id': session_id, 'qr_image': image, 'expires_at': expires,
                'lifetime_seconds': QR_LIFETIME_SECONDS, 'mode': 'fictional-only'}

    def redeem(self, actor: int, session_id: int, challenge: str) -> tuple[int, bool]:
        """One atomic transaction validates challenge and test registration.

        Repeated valid scans by the same user are idempotent and do not append a
        second queue entry. No-show removal, a bad/replaced/expired challenge,
        an outsider or a disabled account fails without modifying the queue.
        """
        if (not isinstance(session_id, int) or isinstance(session_id, bool) or session_id < 1
                or not isinstance(challenge, str) or not CHALLENGE_RE.fullmatch(challenge)):
            raise PermissionDenied('Invalid or expired fictional test QR.')
        digest = hashlib.sha256(challenge.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            self.demo._user_player(c, actor)
            row = c.execute('''SELECT challenge_hash,expires_at FROM unified_test_qr_challenges
                               WHERE session_id=?''', (session_id,)).fetchone()
            now = int(self.demo.clock())
            if not row or row['expires_at'] <= now or not hmac.compare_digest(row['challenge_hash'], digest):
                raise PermissionDenied('Invalid or expired fictional test QR.')
            registration = c.execute('''SELECT 1 FROM session_registrations
                                        WHERE session_id=? AND user_id=? AND payment_state='test_paid' ''',
                                     (session_id, actor)).fetchone()
            if not registration:
                raise PermissionDenied('A confirmed fictional test registration is required.')
            existing = c.execute('SELECT id FROM checkins WHERE session_id=? AND user_id=?',
                                 (session_id, actor)).fetchone()
            if existing:
                return int(existing['id']), False
            checked_at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='microseconds')
            cur = c.execute('INSERT INTO checkins(session_id,user_id,checked_in_at) VALUES(?,?,?)',
                            (session_id, actor, checked_at))
            c.execute('''INSERT INTO queue_entries(session_id,user_id,classification,state,created_at)
                         VALUES(?,?,'initial','waiting',?)''', (session_id, actor, checked_at))
            self.demo._audit(c, actor, 'unified.test_qr_checkin', session_id)
            return int(cur.lastrowid), True
