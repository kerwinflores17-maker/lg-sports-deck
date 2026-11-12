"""Offline payment/reconciliation laboratory for FICTIONAL unified test data.

This is not a GCash integration, a payment provider SDK, or a checkout endpoint.
No QR, real account, credentials, payment, refund, email, or network is used.
The mock gateway has its own disposable SQLite database and an in-memory test
signing key. The existing one-click simulator/UI does not use this laboratory.

The lab rechecks a fake provider's authoritative snapshot before a fictional
booking can be created, keeps event/checkout idempotency keys, and routes paid
but unbookable, mismatched, late, or reversed test cases to manual review. Its
first *processed* fake success may not be the first payment at a real provider.
A real adapter, ordering/late-payment policy, safe money handling, operations,
reconciliation worker, provider terms and independent security review remain
necessary. Never point either database at a real customer or venue database.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from typing import Callable, Iterator

from lg_sports_deck.accounts.service import normalize_test_email
from lg_sports_deck.core.domain import Conflict, Engine, PermissionDenied
from .domain import UnifiedDemo


MAX_EVENT_BYTES = 4096
EVENT_MAX_AGE_SECONDS = 300  # fictional adapter only, not any real provider's format
IDEMPOTENCY_RE = re.compile(r'^test_[A-Za-z0-9_-]{16,64}$', re.ASCII)
INTENT_RE = re.compile(r'^pi_test_[0-9a-f]{32}$', re.ASCII)
PAYMENT_RE = re.compile(r'^fpay_[0-9a-f]{32}$', re.ASCII)
EVENT_RE = re.compile(r'^fevt_[A-Za-z0-9_-]{12,64}$', re.ASCII)
SIG_RE = re.compile(r'^[0-9a-f]{64}$', re.ASCII)


class FictionalGatewayUnavailable(Exception):
    """The local test gateway is unreachable or its fake record is unavailable."""


@dataclass(frozen=True)
class FictionalSnapshot:
    payment_id: str
    reference: str
    amount_minor: int
    currency: str
    status: str
    version: int


class FictionalGateway:
    """Separate, local-only fake gateway; cannot connect to GCash or the network.

    A new disposable DB is initialized with a sentinel. An existing path is
    accepted only if it already bears that sentinel; unknown databases are not
    changed. Reusing the same fake DB survives an app restart. The HMAC key is
    randomly generated in memory and deliberately NOT a real provider key.
    Missed/expired fake callbacks must be recovered by the lab's poll method.
    """
    _SENTINEL = 'LG_SPORTS_DECK_FICTIONAL_GATEWAY_V1'

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        p=Path(path)
        if p.is_symlink():raise ValueError('The fictional gateway path cannot be a symlink.')
        self.path=p.resolve();self.clock=clock;self._event_key=secrets.token_bytes(32)
        self.unavailable=False
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # Atomically create a new empty file or verify the sentinel of a pre-existing
        # fake DB. Never silently add test tables to an unknown existing file.
        try:
            with self.path.open('xb'):
                pass
            new=True
        except FileExistsError:new=False
        if not new:
            try:
                with closing(sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True)) as c:
                    row=c.execute('SELECT value FROM fictional_gateway_identity WHERE name=?',('mode',)).fetchone()
            except sqlite3.Error as exc:
                raise ValueError('Refusing to modify an unknown gateway database.') from exc
            if not row or row[0]!=self._SENTINEL:
                raise ValueError('Refusing to modify an unknown gateway database.')
        else:
            with closing(self._connect()) as c:
                c.executescript('''
                CREATE TABLE fictional_gateway_identity(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE fictional_gateway_payments(
                  payment_id TEXT PRIMARY KEY,
                  reference TEXT NOT NULL UNIQUE,
                  amount_minor INTEGER NOT NULL,
                  currency TEXT NOT NULL,
                  status TEXT NOT NULL,
                  version INTEGER NOT NULL CHECK(version>=1)
                );
                ''')
                c.execute('INSERT INTO fictional_gateway_identity(name,value) VALUES(?,?)',('mode',self._SENTINEL))
                c.commit()

    def _connect(self) -> sqlite3.Connection:
        c=sqlite3.connect(str(self.path),timeout=20,isolation_level=None)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA busy_timeout=20000')
        return c

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c=self._connect()
        try:
            c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()

    def _ensure_available(self) -> None:
        if self.unavailable:raise FictionalGatewayUnavailable('Fictional gateway unavailable.')

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> FictionalSnapshot:
        return FictionalSnapshot(row['payment_id'],row['reference'],row['amount_minor'],row['currency'],row['status'],row['version'])

    def create(self, reference: str, amount_minor: int, currency: str) -> FictionalSnapshot:
        """Idempotently create one fictional provider record per local reference."""
        self._ensure_available()
        if not INTENT_RE.fullmatch(reference) or type(amount_minor) is not int or amount_minor<1 or currency!='PHP':
            raise ValueError('Invalid fictional gateway request.')
        with self._tx() as c:
            row=c.execute('SELECT * FROM fictional_gateway_payments WHERE reference=?',(reference,)).fetchone()
            if row:
                if row['amount_minor']!=amount_minor or row['currency']!=currency:
                    raise Conflict('The fictional gateway reference is already bound to another quote.')
                return self._snapshot(row)
            payment_id='fpay_'+secrets.token_hex(16)
            c.execute('INSERT INTO fictional_gateway_payments VALUES(?,?,?,?,?,?)',
                      (payment_id,reference,amount_minor,currency,'pending',1))
            return self._snapshot(c.execute('SELECT * FROM fictional_gateway_payments WHERE payment_id=?',(payment_id,)).fetchone())

    def retrieve(self, payment_id: str) -> FictionalSnapshot:
        self._ensure_available()
        if not isinstance(payment_id,str) or not PAYMENT_RE.fullmatch(payment_id):
            raise FictionalGatewayUnavailable('Unknown fictional payment.')
        with closing(self._connect()) as c:
            row=c.execute('SELECT * FROM fictional_gateway_payments WHERE payment_id=?',(payment_id,)).fetchone()
        if not row:raise FictionalGatewayUnavailable('Unknown fictional payment.')
        return self._snapshot(row)

    def set_fixture(self, payment_id: str, *, status: str, amount_minor: int | None=None,
                    currency: str | None=None, reference: str | None=None) -> None:
        """Internal fixture mutation; never mounted as an HTTP route."""
        self._ensure_available()
        if status not in ('pending','failed','succeeded','reversed'):
            raise ValueError('Invalid fictional provider state.')
        with self._tx() as c:
            row=c.execute('SELECT * FROM fictional_gateway_payments WHERE payment_id=?',(payment_id,)).fetchone()
            if not row:raise ValueError('Unknown fictional payment.')
            amount=row['amount_minor'] if amount_minor is None else amount_minor
            curr=row['currency'] if currency is None else currency
            ref=row['reference'] if reference is None else reference
            if type(amount) is not int or amount<1 or not isinstance(curr,str) or not isinstance(ref,str):
                raise ValueError('Invalid fictional fixture data.')
            c.execute('''UPDATE fictional_gateway_payments SET status=?,amount_minor=?,currency=?,reference=?,
                         version=version+1 WHERE payment_id=?''',(status,amount,curr,ref,payment_id))

    def event(self,payment_id: str,event_id: str,*,timestamp: int|None=None) -> tuple[bytes,str]:
        """Make an invented signed callback for local tests, not a provider payload."""
        if not PAYMENT_RE.fullmatch(payment_id) or not EVENT_RE.fullmatch(event_id):
            raise ValueError('Invalid fictional callback identifiers.')
        t=int(self.clock()) if timestamp is None else timestamp
        if type(t) is not int:raise ValueError('Invalid fictional callback time.')
        body=json.dumps({'event_id':event_id,'payment_id':payment_id,'timestamp':t},
                        sort_keys=True,separators=(',',':')).encode('ascii')
        return body,hmac.new(self._event_key,body,hashlib.sha256).hexdigest()

    def verify_event(self, body: bytes, signature: str) -> dict:
        """Authenticate the invented local format before parsing or using it."""
        if not isinstance(body,bytes) or len(body)>MAX_EVENT_BYTES or not isinstance(signature,str) or not SIG_RE.fullmatch(signature):
            raise PermissionDenied('Invalid fictional callback.')
        expected=hmac.new(self._event_key,body,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):raise PermissionDenied('Invalid fictional callback.')
        try:
            data=json.loads(body.decode('utf-8'))
            if (not isinstance(data,dict) or set(data)!={'event_id','payment_id','timestamp'}
                or not isinstance(data['event_id'],str) or not EVENT_RE.fullmatch(data['event_id'])
                or not isinstance(data['payment_id'],str) or not PAYMENT_RE.fullmatch(data['payment_id'])
                or type(data['timestamp']) is not int
                or abs(int(self.clock())-data['timestamp'])>EVENT_MAX_AGE_SECONDS):
                raise ValueError
        except (ValueError,UnicodeError,TypeError):
            raise PermissionDenied('Invalid fictional callback.') from None
        return data


SCHEMA = '''
CREATE TABLE IF NOT EXISTS unified_test_payment_intents (
  id TEXT PRIMARY KEY,
  actor_id INTEGER NOT NULL REFERENCES users(id),
  idempotency_key TEXT NOT NULL,
  request_json TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  amount_minor INTEGER NOT NULL CHECK(amount_minor>0 AND amount_minor%100=0),
  currency TEXT NOT NULL CHECK(currency='PHP'),
  gateway_payment_id TEXT UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('initializing','awaiting','failed','confirmed','paid_needs_review','investigate')),
  booking_id INTEGER UNIQUE REFERENCES unified_bookings(id),
  needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0,1)),
  reason_code TEXT,
  last_provider_version INTEGER NOT NULL DEFAULT 0,
  last_snapshot_hash TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_checked_at INTEGER,
  UNIQUE(actor_id,idempotency_key),
  CHECK((state='confirmed' AND booking_id IS NOT NULL) OR (state!='confirmed' AND booking_id IS NULL))
);
CREATE INDEX IF NOT EXISTS unified_test_payment_reconcile
  ON unified_test_payment_intents(state,last_checked_at,id);
CREATE TABLE IF NOT EXISTS unified_test_payment_events (
  event_id TEXT PRIMARY KEY,
  payment_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  received_at INTEGER NOT NULL
);
'''


class FictionalPaymentLab:
    """Optional, manual test-only checkout/event/reconciliation exercise.

    No route or background worker starts this class. Every quoted value is
    server-side; no capacity is held while pending. A local callback is only a
    nudge to re-fetch the fake gateway's record, never a client assertion of paid.
    The booking plus its unsent notification job and test payment decision commit
    atomically in the unified test DB. Real inter-system guarantees are not implied.
    """
    def __init__(self, service: UnifiedDemo, gateway: FictionalGateway):
        if not isinstance(service,UnifiedDemo) or type(gateway) is not FictionalGateway:
            raise ValueError('The payment laboratory only accepts the local fictional gateway.')
        if service.db.path==gateway.path:
            raise ValueError('The fictional gateway needs a separate disposable database.')
        self.service=service;self.db=service.db;self.gateway=gateway;self.clock=service.clock
        with self.db.read() as c:
            c.executescript(SCHEMA)

    @staticmethod
    def _validate_players(players: list[int]) -> list[int]:
        if (not isinstance(players,list) or not 1<=len(players)<=100
            or any(type(p) is not int or p<1 for p in players) or len(players)!=len(set(players))):
            raise ValueError('Choose distinct fictional player IDs.')
        return sorted(players)

    @staticmethod
    def _fictional_users(c, players: list[int]) -> None:
        for player in players:
            user=Engine._user(c,player)
            try:
                if normalize_test_email(user['email'])!=user['email']:raise ValueError
            except ValueError:
                raise PermissionDenied('Only fictional .invalid accounts may use the payment laboratory.') from None

    @staticmethod
    def _serialize(request: dict) -> str:
        return json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True)

    @staticmethod
    def _public(row) -> dict:
        return {'intent_id':row['id'],'payment_id':row['gateway_payment_id'],
                'state':row['state'],'amount_minor':int(row['amount_minor']),
                'currency':row['currency'],'booking_id':row['booking_id'],
                'needs_review':bool(row['needs_review']),'reason_code':row['reason_code'],
                'mode':'fictional-only'}

    def openplay(self, actor: int, key: str, session_id: int, players: list[int]) -> dict:
        if type(session_id) is not int or session_id<1:raise ValueError('Invalid fictional session.')
        return self._begin(actor,key,{'kind':'openplay','session_id':session_id,'players':self._validate_players(players)})

    def court(self, actor: int, key: str, day: date, start_hour: int, duration: int,
              court: int, players: list[int]) -> dict:
        if not isinstance(day,date) or isinstance(day,datetime):
            raise ValueError('Use a fictional date.')
        if any(type(x) is not int for x in (start_hour,duration,court)):
            raise ValueError('Use whole fictional hours and a test court number.')
        # _quote_court_in_tx performs remaining hour/court/range checks.
        return self._begin(actor,key,{'kind':'court','day':day.isoformat(),'start_hour':start_hour,
                                     'duration':duration,'court':court,'players':self._validate_players(players)})

    def _begin(self,actor: int,key: str,request: dict) -> dict:
        if type(actor) is not int or actor<1 or not isinstance(key,str) or not IDEMPOTENCY_RE.fullmatch(key):
            raise ValueError('A fictional player and test idempotency key are required.')
        canonical=self._serialize(request);digest=hashlib.sha256(canonical.encode('ascii')).hexdigest()
        now=int(self.clock());intent_id=None
        with self.db.tx() as c:
            self.service._user_player(c,actor)
            self._fictional_users(c,request['players'])
            row=c.execute('SELECT * FROM unified_test_payment_intents WHERE actor_id=? AND idempotency_key=?',(actor,key)).fetchone()
            if row:
                if not hmac.compare_digest(row['request_hash'],digest):
                    raise Conflict('This fictional checkout key belongs to a different request.')
                intent_id=row['id']
            else:
                if request['kind']=='openplay':
                    _,amount=self.service._quote_openplay_in_tx(c,actor,request['session_id'],request['players'])
                else:
                    *_,amount=self.service._quote_court_in_tx(c,actor,date.fromisoformat(request['day']),
                        request['start_hour'],request['duration'],request['court'],request['players'])
                intent_id='pi_test_'+secrets.token_hex(16)
                c.execute('''INSERT INTO unified_test_payment_intents
                    (id,actor_id,idempotency_key,request_json,request_hash,amount_minor,currency,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'PHP','initializing',?,?)''',
                    (intent_id,actor,key,canonical,digest,amount*100,now,now))
        return self._initialize(intent_id)

    def _initialize(self,intent_id: str) -> dict:
        with self.db.read() as c:row=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
        if not row:raise ValueError('Fictional payment intent not found.')
        if row['gateway_payment_id']:return self._public(row)
        try:
            snap=self.gateway.create(row['id'],int(row['amount_minor']),row['currency'])
        except FictionalGatewayUnavailable:
            with self.db.tx() as c:
                c.execute("UPDATE unified_test_payment_intents SET reason_code='gateway_unavailable',updated_at=? WHERE id=? AND state='initializing'",(int(self.clock()),intent_id))
                return self._public(c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone())
        with self.db.tx() as c:
            current=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
            if current['gateway_payment_id'] not in (None,snap.payment_id):
                raise Conflict('Fictional gateway returned a conflicting reference.')
            c.execute('''UPDATE unified_test_payment_intents SET gateway_payment_id=?,state='awaiting',reason_code=NULL,updated_at=?
                         WHERE id=? AND state='initializing' AND gateway_payment_id IS NULL''',
                      (snap.payment_id,int(self.clock()),intent_id))
            return self._public(c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone())

    def inspect(self, actor: int, intent_id: str) -> dict:
        """Only the test booker or the fictional Superuser may inspect an intent."""
        if not isinstance(intent_id,str) or not INTENT_RE.fullmatch(intent_id):raise ValueError('Invalid fictional intent.')
        with self.db.read() as c:
            user=Engine._user(c,actor)
            row=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
            if not row:raise ValueError('Fictional payment intent not found.')
            if user['role']!='superuser' and row['actor_id']!=actor:
                raise PermissionDenied('This fictional checkout belongs to another account.')
            return self._public(row)

    def callback(self,body: bytes,signature: str) -> dict:
        """Verify and deduplicate a FAKE callback, then query fake provider state.

        The payload contains no trusted success/amount. An unavailable gateway
        leaves the intent pending for a later explicit reconciliation. This method
        is deliberately not mounted on the application's network API.
        """
        event=self.gateway.verify_event(body,signature)
        body_hash=hashlib.sha256(body).hexdigest();now=int(self.clock())
        with self.db.tx() as c:
            previous=c.execute('SELECT * FROM unified_test_payment_events WHERE event_id=?',(event['event_id'],)).fetchone()
            if previous:
                if not hmac.compare_digest(previous['body_hash'],body_hash):
                    raise Conflict('Fictional callback ID was reused with different content.')
            else:
                c.execute('INSERT INTO unified_test_payment_events VALUES(?,?,?,?)',
                          (event['event_id'],event['payment_id'],body_hash,now))
            row=c.execute('SELECT id FROM unified_test_payment_intents WHERE gateway_payment_id=?',(event['payment_id'],)).fetchone()
        if not row:return {'state':'unknown_payment','mode':'fictional-only'}
        return self.reconcile(row['id'])

    def reconcile(self,intent_id: str) -> dict:
        """Manually re-fetch one fake status. This is not a production worker."""
        if not isinstance(intent_id,str) or not INTENT_RE.fullmatch(intent_id):raise ValueError('Invalid fictional intent.')
        with self.db.read() as c:row=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
        if not row:raise ValueError('Fictional payment intent not found.')
        if not row['gateway_payment_id']:
            initialized=self._initialize(intent_id)
            if not initialized['payment_id']:return initialized
            with self.db.read() as c:row=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
        try:snap=self.gateway.retrieve(row['gateway_payment_id'])
        except FictionalGatewayUnavailable:
            with self.db.tx() as c:
                c.execute('''UPDATE unified_test_payment_intents SET reason_code=CASE WHEN needs_review=0 THEN 'gateway_unavailable' ELSE reason_code END,
                             last_checked_at=?,updated_at=? WHERE id=?''',(int(self.clock()),int(self.clock()),intent_id))
                return self._public(c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone())
        return self._apply_snapshot(intent_id,snap)

    def _apply_snapshot(self,intent_id: str,snap: FictionalSnapshot) -> dict:
        now=int(self.clock())
        data=self._serialize({'id':snap.payment_id,'reference':snap.reference,'amount':snap.amount_minor,
                              'currency':snap.currency,'status':snap.status,'version':snap.version})
        fingerprint=hashlib.sha256(data.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            row=c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone()
            if not row:raise ValueError('Fictional payment intent not found.')
            # The fake has a monotonic version. Real gateways may not, so this
            # stale-snapshot protection cannot be assumed for a future provider.
            if snap.version<int(row['last_provider_version']):return self._public(row)
            if snap.version==int(row['last_provider_version']) and row['last_snapshot_hash'] and not hmac.compare_digest(row['last_snapshot_hash'],fingerprint):
                return self._flag(c,row,'inconsistent_snapshot',now)
            mismatch=(snap.payment_id!=row['gateway_payment_id'] or snap.reference!=row['id'] or
                      type(snap.amount_minor) is not int or snap.amount_minor!=row['amount_minor'] or snap.currency!=row['currency'])
            if mismatch:
                return self._flag(c,row,'provider_details_mismatch',now,snap.version,fingerprint)
            # Detect accidental mutation of the quoted request or fractional test
            # amounts before booking. This hash is not a defense against a party
            # with write access to both fields or a production audit mechanism.
            try:
                request_digest=hashlib.sha256(row['request_json'].encode('ascii')).hexdigest()
                intact=(hmac.compare_digest(request_digest,row['request_hash']) and row['amount_minor']%100==0)
            except (UnicodeError,TypeError):intact=False
            if not intact:return self._flag(c,row,'intent_data_changed',now,snap.version,fingerprint)
            state=row['state'];reason=row['reason_code'];review=bool(row['needs_review']);booking=row['booking_id']
            if state=='confirmed':
                # Never silently undo a created booking if the fake gateway later
                # reports a reversal. A human must investigate it.
                if snap.status!='succeeded':review=True;reason='provider_status_changed'
                elif not review:reason=None
            elif state in ('paid_needs_review','investigate'):
                pass  # no automatic retry/clear of a money-related exception
            elif snap.status=='pending':
                if state=='failed':review=True;reason='provider_status_changed'
                else:state='awaiting';reason=None
            elif snap.status=='failed':
                state='failed';reason='fictional_payment_failed'
            elif snap.status=='succeeded':
                if state=='failed':
                    state='paid_needs_review';review=True;reason='paid_after_failure'
                else:
                    request=json.loads(row['request_json'])
                    c.execute('SAVEPOINT fictional_payment_booking')
                    try:
                        self._fictional_users(c,request['players'])
                        if request['kind']=='openplay':
                            result=self.service._book_openplay_in_tx(c,row['actor_id'],request['session_id'],
                                request['players'],expected_amount=int(row['amount_minor'])//100)
                        elif request['kind']=='court':
                            result=self.service._book_court_in_tx(c,row['actor_id'],date.fromisoformat(request['day']),
                                request['start_hour'],request['duration'],request['court'],request['players'],
                                expected_amount=int(row['amount_minor'])//100)
                        else:raise ValueError('Unknown fictional request.')
                    except (Conflict,PermissionDenied,ValueError,sqlite3.IntegrityError):
                        c.execute('ROLLBACK TO fictional_payment_booking');c.execute('RELEASE fictional_payment_booking')
                        state='paid_needs_review';review=True;reason='booking_unavailable_or_changed'
                    else:
                        c.execute('RELEASE fictional_payment_booking')
                        state='confirmed';reason=None;booking=result['booking_id']
            else:
                review=True;reason='provider_status_changed';state='investigate'
            c.execute('''UPDATE unified_test_payment_intents SET state=?,booking_id=?,needs_review=?,reason_code=?,
                         last_provider_version=?,last_snapshot_hash=?,last_checked_at=?,updated_at=? WHERE id=?''',
                      (state,booking,int(review),reason,snap.version,fingerprint,now,now,intent_id))
            return self._public(c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(intent_id,)).fetchone())

    def _flag(self,c,row,reason: str,now: int,version: int|None=None,fingerprint: str|None=None) -> dict:
        state='confirmed' if row['state']=='confirmed' else 'investigate'
        c.execute('''UPDATE unified_test_payment_intents SET state=?,needs_review=1,reason_code=?,
                     last_provider_version=?,last_snapshot_hash=?,last_checked_at=?,updated_at=? WHERE id=?''',
                  (state,reason,row['last_provider_version'] if version is None else version,
                   row['last_snapshot_hash'] if fingerprint is None else fingerprint,now,now,row['id']))
        return self._public(c.execute('SELECT * FROM unified_test_payment_intents WHERE id=?',(row['id'],)).fetchone())

    def reconcile_pending(self,limit: int=100) -> list[dict]:
        """Manual bounded poll for test intents; no timer, automatic worker or network."""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Invalid fictional poll limit.')
        with self.db.read() as c:
            ids=[r['id'] for r in c.execute('''SELECT id FROM unified_test_payment_intents
                WHERE state IN ('initializing','awaiting','failed') ORDER BY COALESCE(last_checked_at,0),id LIMIT ?''',(limit,))]
        return [self.reconcile(i) for i in ids]
