"""Durable *simulated* notification jobs for fictional integrated tests.

No network transport, SMTP, Gmail credentials, real addresses or delivery are
implemented here.  Jobs are captured atomically with fictional business events;
a deliberately manual simulator exercises claims, backoff and ambiguous outcomes.
SMTP acceptance is not inbox delivery, and a live provider cannot be made exactly
once by a local database transaction. This is NOT a production mail worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import hashlib
import re
import secrets
import time
from typing import Callable, Literal
from urllib.parse import urlsplit

from lg_sports_deck.accounts.service import normalize_test_email
from lg_sports_deck.core.domain import Database


# Each event has a stable unique key. Triggers keep test jobs in the same SQLite
# transaction as the event, even when the existing core recorder creates outbox
# rows. This primary outbox includes only unified Open Play match rows. Rental
# sample replay recipients use a separate, provisional test-only extra bucket.
# Legacy player/waitlist/match jobs
# have idempotent backfills; venue-booking jobs have no historical backfill.
# Nothing here sends mail.
SCHEMA = r"""
CREATE TABLE IF NOT EXISTS unified_test_mail_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('booking_confirmation','waitlist_open','replay','result')),
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','processing','retry_wait','simulated','uncertain','cancelled','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT,
  lease_until INTEGER,
  failure_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  CHECK(attempts>=0),
  CHECK((state='processing' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
     OR (state<>'processing' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE INDEX IF NOT EXISTS unified_test_mail_due
  ON unified_test_mail_jobs(state,available_at,id);
CREATE TABLE IF NOT EXISTS unified_test_mail_receipts (
  job_id INTEGER PRIMARY KEY REFERENCES unified_test_mail_jobs(id),
  simulated_at INTEGER NOT NULL
);
-- A *fictional*, replaceable destination for venue booking notifications.
-- It is distinct from the future SMTP sender and the booker's address.
CREATE TABLE IF NOT EXISTS unified_test_booking_notice_destination (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  recipient_email TEXT,
  owner_user_id INTEGER REFERENCES users(id),
  CHECK((recipient_email IS NULL AND owner_user_id IS NULL) OR
        (recipient_email IS NOT NULL AND owner_user_id IS NOT NULL))
);
INSERT OR IGNORE INTO unified_test_booking_notice_destination(singleton) VALUES(1);

CREATE TRIGGER IF NOT EXISTS unified_test_mail_booking
AFTER INSERT ON unified_bookings
BEGIN
  INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
  SELECT 'booking:'||NEW.id, u.id, u.email, 'booking_confirmation',
    '[TEST ONLY] Fictional booking confirmation',
    'TEST ONLY: '||printf('TEST-%05d',NEW.id)||' was recorded for '||NEW.day||
    ' with a simulated amount of PHP '||NEW.amount||'. No real charge, booking or email.',
    NEW.created_at,NEW.created_at FROM users u WHERE u.id=NEW.booker_id;
END;

-- Separate venue notice: captured in the same fictional paid-booking
-- transaction as the player's own notice. The destination is snapshotted, not
-- changed retroactively when settings change. There is no historical backfill.
CREATE TRIGGER IF NOT EXISTS unified_test_mail_venue_booking
AFTER INSERT ON unified_bookings
WHEN NEW.state='simulated_paid'
BEGIN
  INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
  SELECT 'venue_booking:'||NEW.id, owner.id, dest.recipient_email, 'booking_confirmation',
    '[TEST ONLY] Venue notice: simulated paid booking',
    'TEST ONLY: '||printf('TEST-%05d',NEW.id)||' is a simulated paid '||
    CASE WHEN NEW.kind='openplay' THEN 'Open Play' ELSE 'court rental' END||' booking for '||NEW.day||
    ', '||printf('%02d:%02d',NEW.start_min/60,NEW.start_min%60)||'–'||
    printf('%02d:%02d',NEW.end_min/60,NEW.end_min%60)||
    CASE WHEN NEW.court_id IS NULL THEN '' ELSE ' on Court '||printf('%02d',NEW.court_id) END||
    '. Test booker: '||booker.display_name||'. Simulated amount: PHP '||NEW.amount||
    '. No real payment, booking or email.',
    NEW.created_at,NEW.created_at
  FROM unified_test_booking_notice_destination dest
  JOIN users owner ON owner.id=dest.owner_user_id AND owner.role='superuser' AND owner.enabled=1
  JOIN users booker ON booker.id=NEW.booker_id
  WHERE dest.singleton=1 AND dest.recipient_email IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS unified_test_mail_waitlist
AFTER INSERT ON unified_notifications
BEGIN
  INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
  SELECT 'waitlist:'||NEW.id, u.id, u.email, 'waitlist_open',
    '[TEST ONLY] Fictional place opened', NEW.message, NEW.created_at, NEW.created_at
  FROM users u WHERE u.id=NEW.user_id;
END;

CREATE TRIGGER IF NOT EXISTS unified_test_mail_match
AFTER INSERT ON outbox
WHEN EXISTS (SELECT 1 FROM matches m JOIN unified_sessions s ON s.session_id=m.session_id
             WHERE m.id=NEW.match_id AND m.service='openplay')
BEGIN
  INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
  VALUES('match:'||NEW.match_id||':'||NEW.user_id||':'||NEW.kind, NEW.user_id, NEW.recipient,
         NEW.kind, NEW.subject, NEW.body, COALESCE(CAST(strftime('%s',NEW.created_at) AS INTEGER),0),
         COALESCE(CAST(strftime('%s',NEW.created_at) AS INTEGER),0));
END;

-- Additive migration of pre-existing fictional events. These are queued test
-- records, never actual messages; running this more than once is safe.
INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
SELECT 'booking:'||b.id,u.id,u.email,'booking_confirmation','[TEST ONLY] Fictional booking confirmation',
       'TEST ONLY: '||printf('TEST-%05d',b.id)||' was recorded for '||b.day||
       ' with a simulated amount of PHP '||b.amount||'. No real charge, booking or email.',
       b.created_at,b.created_at FROM unified_bookings b JOIN users u ON u.id=b.booker_id;
INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
SELECT 'waitlist:'||n.id,u.id,u.email,'waitlist_open','[TEST ONLY] Fictional place opened',
       n.message,n.created_at,n.created_at FROM unified_notifications n JOIN users u ON u.id=n.user_id;
INSERT OR IGNORE INTO unified_test_mail_jobs(event_key,user_id,recipient,kind,subject,body,created_at,updated_at)
SELECT 'match:'||o.match_id||':'||o.user_id||':'||o.kind,o.user_id,o.recipient,o.kind,o.subject,o.body,
       COALESCE(CAST(strftime('%s',o.created_at) AS INTEGER),0),
       COALESCE(CAST(strftime('%s',o.created_at) AS INTEGER),0)
FROM outbox o JOIN matches m ON m.id=o.match_id
JOIN unified_sessions s ON s.session_id=m.session_id WHERE m.service='openplay';
"""


def install_test_schema(db: Database) -> None:
    """Install additive tables/triggers after the unified tables have been created."""
    with db.read() as c:
        c.executescript(SCHEMA)


MAIL_BUCKETS = {
    'primary': ('unified_test_mail_jobs','unified_test_mail_receipts'),
    'extra': ('unified_test_extra_mail_jobs','unified_test_extra_mail_receipts'),
}


def _mail_bucket(bucket: str) -> tuple[str,str]:
    if bucket not in MAIL_BUCKETS:raise ValueError('Unknown fictional mail bucket.')
    return MAIL_BUCKETS[bucket]


def render_test_mime(db: Database, job_id: int, app_origin: str, sender: str, *, bucket: str='primary') -> EmailMessage:
    """Render, but NEVER send, one fictional message to a single .invalid user.

    An HTTPS .invalid origin is required so a test message cannot accidentally
    contain a production URL. The assignment/replay/result link is a login-gated UI route,
    not an authentication token; server-side participant authorization still
    applies. Actual retention is not enabled. This renderer creates no socket.
    """
    try:
        normalized_sender=normalize_test_email(sender)
        parsed=urlsplit(app_origin)
        host=parsed.hostname or ''
        valid_host=bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?\.invalid',host,re.ASCII)) and '..' not in host
        port=parsed.port
        if (normalized_sender!=sender or parsed.scheme!='https' or not valid_host or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('','/') or parsed.query or parsed.fragment
            or any(ord(ch)<33 for ch in app_origin)):
            raise ValueError('Use an explicit HTTPS .invalid test origin and fictional sender.')
    except (TypeError,ValueError) as exc:
        raise ValueError('Use an explicit HTTPS .invalid test origin and fictional sender.') from exc
    origin=f'https://{host}' + (f':{port}' if port is not None else '')
    table,_=_mail_bucket(bucket)  # fixed allowlist, never a caller-provided SQL identifier
    with db.read() as c:
        if bucket=='primary':
            row=c.execute('''SELECT j.*,o.match_id FROM unified_test_mail_jobs j LEFT JOIN outbox o
                             ON j.event_key='match:'||o.match_id||':'||o.user_id||':'||o.kind
                             WHERE j.id=?''',(job_id,)).fetchone()
        else:
            row=c.execute(f'SELECT * FROM {table} WHERE id=?',(job_id,)).fetchone()
    if not row:raise ValueError('Fictional notification job not found.')
    recipient=normalize_test_email(row['recipient'])
    if recipient!=row['recipient']:raise ValueError('Invalid fictional recipient.')
    message=EmailMessage()
    message['From']=normalized_sender;message['To']=recipient;message['Subject']=row['subject']
    digest=hashlib.sha256(row['event_key'].encode('utf-8')).hexdigest()
    message['Message-ID']=f'<{digest}@lg-sports-deck.invalid>'
    message['X-LG-Test-Only']='yes'
    body=row['body']
    if row['kind'] in ('replay','result','queue_assignment','rental_replay'):
        if not row['match_id'] or row['match_id']<1:raise ValueError('Fictional match link is unavailable.')
        body+=f"\n\nFictional match page: {origin}/#match-{row['match_id']}\nSign-in and participant authorization are required. Automatic real-media expiry is not implemented."
    message.set_content(body)
    return message


@dataclass(frozen=True)
class TestClaim:
    job_id: int
    lease_token: str
    recipient: str
    kind: str
    attempt: int
    lease_until: int
    bucket: str='primary'


class SimulatedMailWorker:
    """Manual, local-only job-state simulation; it can never send an email.

    An expired in-flight lease and an ambiguous result are marked uncertain,
    rather than blindly retried. Only a known pre-delivery failure is retried.
    Receipt records and simulated completion are committed together in SQLite.
    Real SMTP requires a separately designed provider, credentials, monitoring,
    reconciliation and legal/privacy review; this class is not such a provider.
    """
    def __init__(self, db: Database, clock: Callable[[], float] = time.time,
                 *, lease_seconds: int = 120, max_attempts: int = 5, bucket: str='primary'):
        if not 1 <= lease_seconds <= 3600 or not 1 <= max_attempts <= 10:
            raise ValueError('Invalid test-worker limits.')
        self.jobs,self.receipts=_mail_bucket(bucket);self.bucket=bucket
        self.db=db;self.clock=clock;self.lease_seconds=lease_seconds;self.max_attempts=max_attempts

    def claim(self) -> TestClaim | None:
        """Reserve one due fictional job; expired in-flight jobs fail closed."""
        now=int(self.clock())
        with self.db.tx() as c:
            # We cannot know whether an interrupted external transport accepted
            # a message. Never silently retry a potentially delivered attempt.
            c.execute(f"""UPDATE {self.jobs}
                SET state='uncertain',failure_code='lease_expired',lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE state='processing' AND lease_until<=?""",(now,now))
            c.execute(f"""UPDATE {self.jobs}
                SET state='cancelled',failure_code='disabled_user',updated_at=?
                WHERE state IN ('queued','retry_wait') AND user_id IN (SELECT id FROM users WHERE enabled=0)""",(now,))
            while True:
                row=c.execute(f"""SELECT j.* FROM {self.jobs} j JOIN users u ON u.id=j.user_id
                    WHERE j.state IN ('queued','retry_wait') AND j.available_at<=? AND u.enabled=1
                    ORDER BY j.id LIMIT 1""",(now,)).fetchone()
                if not row:return None
                try:
                    normalized=normalize_test_email(row['recipient'])
                    if normalized!=row['recipient']:raise ValueError('Unnormalized test address')
                except ValueError:
                    c.execute(f"UPDATE {self.jobs} SET state='failed',failure_code='invalid_test_recipient',updated_at=? WHERE id=?",(now,row['id']))
                    continue
                token=secrets.token_urlsafe(24)
                changed=c.execute(f"""UPDATE {self.jobs} SET state='processing',attempts=attempts+1,
                    lease_token=?,lease_until=?,failure_code=NULL,updated_at=?
                    WHERE id=? AND state IN ('queued','retry_wait')""",(token,now+self.lease_seconds,now,row['id'])).rowcount
                if changed:
                    return TestClaim(int(row['id']),token,row['recipient'],row['kind'],int(row['attempts'])+1,now+self.lease_seconds,self.bucket)

    def finish(self, claim: TestClaim, outcome: Literal['success','transient','permanent','unknown'],
               *, fake_transport_attempted: bool=False) -> str:
        """Finish a simulated attempt using a compare-and-set lease token.

        Error text is never persisted, only a fixed code, so simulated exceptions
        cannot leak credentials or message contents to an operations log. A
        separate test-only loopback SMTP harness may set fake_transport_attempted;
        a user disabled during that in-flight attempt is uncertain if the fake
        sink might have accepted data. This method never opens a socket itself.
        """
        if outcome not in ('success','transient','permanent','unknown'):
            raise ValueError('Invalid fictional delivery outcome.')
        if claim.bucket!=self.bucket:return 'stale'
        now=int(self.clock())
        with self.db.tx() as c:
            row=c.execute(f'SELECT * FROM {self.jobs} WHERE id=?',(claim.job_id,)).fetchone()
            if not row or row['state']!='processing' or not secrets.compare_digest(row['lease_token'] or '',claim.lease_token):
                return 'stale'
            if row['lease_until']<=now:
                state,code='uncertain','lease_expired'
            elif not c.execute('SELECT 1 FROM users WHERE id=? AND enabled=1',(row['user_id'],)).fetchone():
                if fake_transport_attempted and outcome in ('success','unknown'):
                    state,code='uncertain','disabled_during_test_submission'
                else:
                    state,code='cancelled','disabled_user'
            elif outcome=='success':
                state,code='simulated',None
                c.execute(f'INSERT OR IGNORE INTO {self.receipts}(job_id,simulated_at) VALUES(?,?)',(claim.job_id,now))
            elif outcome=='transient' and row['attempts']<self.max_attempts:
                state,code='retry_wait','test_transient'
            elif outcome=='unknown':state,code='uncertain','test_unknown'
            else:state,code='failed','test_attempt_limit' if outcome=='transient' else 'test_permanent'
            next_at=now+min(3600,60*(2**max(0,int(row['attempts'])-1))) if state=='retry_wait' else 0
            c.execute(f'''UPDATE {self.jobs} SET state=?,failure_code=?,available_at=?,lease_token=NULL,
                         lease_until=NULL,updated_at=? WHERE id=?''',(state,code,next_at,now,claim.job_id))
            return state

    def run_one(self, outcome: Literal['success','transient','permanent','unknown']='success') -> tuple[int,str] | None:
        """Exercise one job entirely in SQLite; does not create a socket."""
        claim=self.claim()
        return (claim.job_id,self.finish(claim,outcome)) if claim else None
