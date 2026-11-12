"""Superuser-only configuration placeholders for the FICTIONAL portal.

Only `.invalid` sender/venue-recipient addresses and system-generated fictional
payment QR images are allowed here. No user can upload a real QR, provide a Gmail
secret, connect a payment provider, send an email or accept a payment through this
module. The generated payload cannot receive GCash and is not a check-in QR.

Real sender/provider settings require a separate, security-reviewed design:
verified ownership, secure secret management, reauthentication/MFA, audited
activation/rollback, and a policy preserving pending transactions under their
original provider configuration. This prototype is not that implementation.
"""
from __future__ import annotations

import re
import secrets

from lg_sports_deck.accounts.service import normalize_test_email
from lg_sports_deck.core.domain import Conflict, Engine, PermissionDenied
from .domain import UnifiedDemo
from .qr_test import FictionalQR


PAYMENT_TEST_PREFIX='LGSD-PAYMENT-TEST-ONLY'
TOKEN_RE=re.compile(r'^[A-Za-z0-9_-]{32}$',re.ASCII)


class FictionalIntegrationSettings:
    """Editable fake placeholders; only an enabled Superuser may view/change.

    A monotonically increasing revision guards against stale concurrent writes.
    The fake QR and sender do not affect checkout or send mail. The optional
    fictional venue recipient causes an additional *unsent* test job on a
    fictional paid booking; it is separate from the test booker's own notice.
    """
    def __init__(self,service: UnifiedDemo):
        self.service=service;self.db=service.db
        with self.db.read() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS unified_test_integration_settings (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              sender_email TEXT,
              payment_qr_token TEXT,
              revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
              updated_at INTEGER,
              updated_by INTEGER REFERENCES users(id)
            );
            INSERT OR IGNORE INTO unified_test_integration_settings(singleton,revision) VALUES(1,0);
            ''')

    @staticmethod
    def _superuser(c,actor: int) -> None:
        if Engine._user(c,actor)['role']!='superuser':
            raise PermissionDenied('Only the fictional Superuser may access test payment and email settings.')

    @staticmethod
    def _revision(value: int) -> int:
        if type(value) is not int or value<0:raise ValueError('Invalid fictional settings revision.')
        return value

    @staticmethod
    def _read(c):
        return c.execute('''SELECT s.*,dest.recipient_email AS booking_recipient_email
            FROM unified_test_integration_settings s
            JOIN unified_test_booking_notice_destination dest ON dest.singleton=s.singleton
            WHERE s.singleton=1''').fetchone()

    @staticmethod
    def _view(row) -> dict:
        token=row['payment_qr_token']
        if token is not None and (not isinstance(token,str) or not TOKEN_RE.fullmatch(token)):
            raise Conflict('Fictional QR settings are invalid; inspect the test database.')
        return {'sender_email':row['sender_email'],
                'booking_recipient_email':row['booking_recipient_email'],
                'payment_qr_image':FictionalQR._png_data_url(f'{PAYMENT_TEST_PREFIX}:{token}') if token else None,
                'revision':int(row['revision']),'updated_at':row['updated_at'],
                'mode':'fictional-only',
                'message':'Fictional placeholders only. No real GCash or Gmail account is connected.'}

    def view(self,actor: int) -> dict:
        with self.db.read() as c:
            self._superuser(c,actor)
            row=self._read(c)
        return self._view(row)

    def replace_sender(self,actor: int,expected_revision: int,sender_email: str) -> dict:
        """Change/clear only a fictional sender identity; no SMTP configuration."""
        self._revision(expected_revision)
        if not isinstance(sender_email,str) or len(sender_email)>254:
            raise ValueError('Use a fictional .invalid sender address or leave it blank.')
        try:email=normalize_test_email(sender_email) if sender_email.strip() else None
        except ValueError:
            raise ValueError('Use a fictional .invalid sender address or leave it blank.') from None
        with self.db.tx() as c:
            self._superuser(c,actor)
            row=self._read(c)
            if row['revision']!=expected_revision:
                raise Conflict('The fictional settings changed. Refresh before saving.')
            if row['sender_email']!=email:
                c.execute('''UPDATE unified_test_integration_settings SET sender_email=?,revision=revision+1,
                             updated_at=?,updated_by=? WHERE singleton=1''',
                          (email,int(self.service.clock()),actor))
                self.service._audit(c,actor,'unified.test_sender_changed')
            result=self._read(c)
        return self._view(result)

    def replace_booking_recipient(self,actor: int,expected_revision: int,recipient_email: str) -> dict:
        """Set/clear a fictional venue inbox; this never configures a sender.

        Only future fictional paid bookings queue a separate unsent venue job.
        Existing test jobs keep the address captured when they were created;
        the policy for pending real emails must be decided before live use.
        """
        self._revision(expected_revision)
        if not isinstance(recipient_email,str) or len(recipient_email)>254:
            raise ValueError('Use a fictional .invalid recipient or leave it blank.')
        try:email=normalize_test_email(recipient_email) if recipient_email.strip() else None
        except ValueError:
            raise ValueError('Use a fictional .invalid recipient or leave it blank.') from None
        with self.db.tx() as c:
            self._superuser(c,actor)
            row=self._read(c)
            if row['revision']!=expected_revision:
                raise Conflict('The fictional settings changed. Refresh before saving.')
            if row['booking_recipient_email']!=email:
                c.execute('''UPDATE unified_test_booking_notice_destination SET recipient_email=?,owner_user_id=?
                             WHERE singleton=1''',(email,actor if email else None))
                c.execute('''UPDATE unified_test_integration_settings SET revision=revision+1,
                             updated_at=?,updated_by=? WHERE singleton=1''',
                          (int(self.service.clock()),actor))
                self.service._audit(c,actor,'unified.test_booking_recipient_changed')
            result=self._read(c)
        return self._view(result)

    def venue_notices(self,actor: int) -> list[dict]:
        """List only this Superuser's most recent unsent/simulated venue jobs."""
        with self.db.read() as c:
            self._superuser(c,actor)
            rows=c.execute('''SELECT recipient,subject,body,state,created_at FROM unified_test_mail_jobs
                WHERE user_id=? AND substr(event_key,1,14)='venue_booking:' ORDER BY id DESC LIMIT 50''',(actor,)).fetchall()
            return [dict(r) for r in rows]

    def replace_payment_qr(self,actor: int,expected_revision: int) -> dict:
        """Replace a system-generated *fake* image; this cannot accept money."""
        self._revision(expected_revision)
        token=secrets.token_urlsafe(24)
        if not TOKEN_RE.fullmatch(token):raise RuntimeError('Fictional QR generation failed.')
        with self.db.tx() as c:
            self._superuser(c,actor)
            row=self._read(c)
            if row['revision']!=expected_revision:
                raise Conflict('The fictional settings changed. Refresh before replacing the QR.')
            c.execute('''UPDATE unified_test_integration_settings SET payment_qr_token=?,revision=revision+1,
                         updated_at=?,updated_by=? WHERE singleton=1''',
                      (token,int(self.service.clock()),actor))
            self.service._audit(c,actor,'unified.test_payment_qr_replaced')
            result=self._read(c)
        return self._view(result)
