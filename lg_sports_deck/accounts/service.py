"""Account and access-control primitives for fictional INTERNAL tests only.

All account addresses must end in .invalid. There is no real email verification,
password recovery, MFA, customer onboarding or production deployment security.
The existing core DB is reused so grants, user roles and queue authorization are
checked against the same server-side records. No token/password is logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import Callable

from argon2 import PasswordHasher, Type, exceptions as argon_errors
from lg_sports_deck.core.domain import Conflict, Database, Engine, PermissionDenied, timestamp


TEST_NOTICE_VERSION='fictional-recording-notice-v1'
SESSION_SECONDS=12*60*60
FAILURE_LIMIT=5
FAILURE_WINDOW_SECONDS=5*60
# Exact individually grantable test actions. Recording access and granting staff
# permissions are intentionally absent: only participants/Superuser access media;
# only Superuser can manage staff permissions.
STAFF_GRANTABLE=frozenset({
    'bookings.view','rankings.view','queue.view','queue.manage','session.view',
    'score.enter','settings.rates.edit',
})
EMAIL_RE=re.compile(r'^[a-z0-9][a-z0-9._+\-]{0,63}@[a-z0-9](?:[a-z0-9.\-]{0,120}[a-z0-9])?\.invalid$',re.ASCII)


class AuthenticationFailed(Exception):pass
class RateLimited(Exception):pass


@dataclass(frozen=True)
class Principal:
    id: int
    email: str
    name: str
    role: str
    session_hash: str
    csrf_hash: str
    expires_at: int


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def normalize_test_email(value: str) -> str:
    if not isinstance(value,str):raise ValueError('Use a fictional .invalid email address.')
    email=value.strip().lower()
    if len(email)>254 or not EMAIL_RE.fullmatch(email) or '..' in email:
        raise ValueError('Use a fictional .invalid email address.')
    return email


def validate_password(value: str) -> None:
    try:valid=isinstance(value,str) and 12<=len(value)<=128 and len(value.encode('utf-8'))<=512
    except UnicodeError:valid=False
    if not valid:raise ValueError('Test password must contain 12–128 characters and at most 512 UTF-8 bytes.')


def safe_name(value: str) -> str:
    if not isinstance(value,str):raise ValueError('A fictional display name is required.')
    name=value.strip()
    try:name.encode('utf-8')
    except UnicodeError:raise ValueError('Use a valid fictional display name.')
    if not name or len(name)>80 or any(ord(ch)<32 or ord(ch)==127 for ch in name):
        raise ValueError('Use a fictional display name of 1–80 characters.')
    return name


class Accounts:
    def __init__(self, db: Database, clock: Callable[[],float]=time.time, *, extra_grantable: frozenset[str]=frozenset()):
        self.db=db;self.clock=clock
        # Application-defined actions only. Replay and permission delegation are
        # never grantable, even if an embedding test application requests them.
        forbidden={'recordings.view','recording.view','replay.view','staff.permissions'}
        if forbidden.intersection(extra_grantable):raise ValueError('Forbidden test permission.')
        self.grantable=STAFF_GRANTABLE | frozenset(extra_grantable)
        # Argon2id, unique salt per hash. Parameters are test configuration,
        # not a substitute for a production security review or hardware sizing.
        self.passwords=PasswordHasher(time_cost=2,memory_cost=19456,parallelism=1,
                                      hash_len=32,salt_len=16,type=Type.ID)
        self._dummy_hash=self.passwords.hash(secrets.token_urlsafe(24))
        self._initialize()

    def _initialize(self):
        with self.db.read() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
            CREATE TABLE IF NOT EXISTS auth_failure_buckets (
              bucket_hash TEXT PRIMARY KEY, window_start INTEGER NOT NULL, failures INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, target_id INTEGER,
              event TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_notices (
              user_id INTEGER NOT NULL REFERENCES users(id), version TEXT NOT NULL,
              accepted_at INTEGER NOT NULL, PRIMARY KEY(user_id,version)
            );
            CREATE TABLE IF NOT EXISTS auth_test_settings (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), open_rate INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO auth_test_settings(singleton,open_rate) VALUES(1,200);
            CREATE TABLE IF NOT EXISTS auth_test_bookings (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              booker_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL,
              state TEXT NOT NULL CHECK(state='simulated_paid'), created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_test_booking_players (
              booking_id INTEGER NOT NULL REFERENCES auth_test_bookings(id),
              user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(booking_id,user_id)
            );
            ''')

    def now(self) -> int:return int(self.clock())

    def _verify(self, encoded: str, password: str) -> bool:
        try:return self.passwords.verify(encoded,password)
        except (argon_errors.VerifyMismatchError,argon_errors.VerificationError,argon_errors.InvalidHashError,TypeError,UnicodeError):return False

    def _audit(self,c,actor,event,target=None):
        c.execute('INSERT INTO auth_audit(actor_id,target_id,event,created_at) VALUES(?,?,?,?)',
                  (actor,target,event,self.now()))

    def bootstrap_test_superuser(self,email: str,name: str,password: str) -> int:
        """Offline test bootstrap only; deliberately not an HTTP endpoint."""
        email=normalize_test_email(email);name=safe_name(name);validate_password(password)
        encoded=self.passwords.hash(password)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM users WHERE role='superuser' LIMIT 1").fetchone():
                raise Conflict('A fictional Superuser already exists.')
            cur=c.execute('INSERT INTO users(email,display_name,password_hash,role) VALUES(?,?,?,?)',(email,name,encoded,'superuser'))
            uid=int(cur.lastrowid);self._audit(c,uid,'fixture.superuser_created',uid)
            return uid

    def register_test_player(self,email: str,name: str,password: str) -> int:
        email=normalize_test_email(email);name=safe_name(name);validate_password(password)
        encoded=self.passwords.hash(password)
        try:
            with self.db.tx() as c:
                cur=c.execute('INSERT INTO users(email,display_name,password_hash,role) VALUES(?,?,?,?)',(email,name,encoded,'player'))
                uid=int(cur.lastrowid);self._audit(c,uid,'fixture.player_registered',uid);return uid
        except sqlite3.IntegrityError as exc:raise Conflict('The fictional test account cannot be created.') from exc

    def create_test_staff(self,actor: int,email: str,name: str,password: str,role: str) -> int:
        if role not in {'admin','manager'}:raise ValueError('Staff role must be admin or manager.')
        email=normalize_test_email(email);name=safe_name(name);validate_password(password)
        encoded=self.passwords.hash(password)
        try:
            with self.db.tx() as c:
                if Engine._user(c,actor)['role']!='superuser':raise PermissionDenied('Only Superuser can create staff accounts.')
                cur=c.execute('INSERT INTO users(email,display_name,password_hash,role) VALUES(?,?,?,?)',(email,name,encoded,role))
                uid=int(cur.lastrowid);self._audit(c,actor,'fixture.staff_created',uid);return uid
        except sqlite3.IntegrityError as exc:raise Conflict('The fictional test account cannot be created.') from exc

    def replace_permissions(self,actor: int,target: int,permissions: list[str]) -> None:
        if not isinstance(permissions,list) or len(permissions)!=len(set(permissions)) or not set(permissions).issubset(self.grantable):
            raise ValueError('Unknown, duplicated or forbidden staff permission.')
        with self.db.tx() as c:
            if Engine._user(c,actor)['role']!='superuser':raise PermissionDenied('Only Superuser can set staff permissions.')
            if Engine._user(c,target)['role'] not in ('admin','manager'):raise ValueError('Choose an Admin or Manager.')
            c.execute('DELETE FROM staff_permissions WHERE user_id=?',(target,))
            c.executemany('INSERT INTO staff_permissions(user_id,permission) VALUES(?,?)',[(target,p) for p in sorted(permissions)])
            self._audit(c,actor,'permissions.replaced',target)

    def list_test_staff(self,actor: int) -> list[dict]:
        with self.db.read() as c:
            if Engine._user(c,actor)['role']!='superuser':raise PermissionDenied('Only Superuser can view staff permission settings.')
            people=[dict(x) for x in c.execute("SELECT id,email,display_name,role,enabled FROM users WHERE role IN ('admin','manager') ORDER BY id")]
            for p in people:p['permissions']=[r['permission'] for r in c.execute('SELECT permission FROM staff_permissions WHERE user_id=? ORDER BY permission',(p['id'],))]
            return people

    def disable_test_user(self,actor: int,target: int) -> None:
        with self.db.tx() as c:
            if Engine._user(c,actor)['role']!='superuser':raise PermissionDenied('Only Superuser can disable a test account.')
            u=Engine._user(c,target)
            if u['role']=='superuser':raise PermissionDenied('Superuser disable/recovery is not implemented.')
            c.execute('UPDATE users SET enabled=0 WHERE id=?',(target,))
            c.execute('UPDATE auth_sessions SET revoked=1 WHERE user_id=?',(target,))
            self._audit(c,actor,'account.disabled',target)

    @staticmethod
    def _bucket_key(kind: str,value: str) -> str:return token_hash(kind+':'+value)

    def _bucket_full(self,c,key: str,now: int) -> bool:
        row=c.execute('SELECT window_start,failures FROM auth_failure_buckets WHERE bucket_hash=?',(key,)).fetchone()
        return bool(row and now-int(row['window_start'])<FAILURE_WINDOW_SECONDS and int(row['failures'])>=FAILURE_LIMIT)

    def _bucket_failed(self,c,key: str,now: int) -> None:
        row=c.execute('SELECT window_start FROM auth_failure_buckets WHERE bucket_hash=?',(key,)).fetchone()
        if not row or now-int(row['window_start'])>=FAILURE_WINDOW_SECONDS:
            c.execute('INSERT INTO auth_failure_buckets(bucket_hash,window_start,failures) VALUES(?,?,1) ON CONFLICT(bucket_hash) DO UPDATE SET window_start=excluded.window_start,failures=1',(key,now))
        else:c.execute('UPDATE auth_failure_buckets SET failures=failures+1 WHERE bucket_hash=?',(key,))

    def login(self,email: str,password: str,peer: str) -> tuple[str,str,int]:
        # Same failure response for missing, disabled and wrong-password accounts.
        normalized=(email.strip().lower()[:254] if isinstance(email,str) else '')
        if not EMAIL_RE.fullmatch(normalized) or '..' in normalized:normalized='invalid-test-address'
        try:password_ok=isinstance(password,str) and len(password)<=128 and len(password.encode('utf-8'))<=512
        except UnicodeError:password_ok=False
        if not password_ok:password='invalid-test-password'
        peer=(peer or 'unknown')[:128]
        email_key=self._bucket_key('email',normalized);peer_key=self._bucket_key('peer',peer)
        now=self.now();outcome='invalid';result=None
        # Holding the SQLite write transaction while verifying is deliberate for
        # these small tests: simultaneous attempts cannot outrun the counter.
        # A scalable deployment needs a dedicated shared rate-limiting service.
        with self.db.tx() as c:
            if self._bucket_full(c,email_key,now) or self._bucket_full(c,peer_key,now):
                outcome='limited';self._audit(c,None,'login.rate_limited')
            else:
                user=c.execute('SELECT * FROM users WHERE email=? COLLATE NOCASE',(normalized,)).fetchone()
                encoded=user['password_hash'] if user and user['enabled'] else self._dummy_hash
                valid=self._verify(encoded,password)
                if not user or not user['enabled'] or not valid:
                    self._bucket_failed(c,email_key,now);self._bucket_failed(c,peer_key,now)
                    self._audit(c,None,'login.failed');outcome='invalid'
                else:
                    c.execute('DELETE FROM auth_failure_buckets WHERE bucket_hash=?',(email_key,))
                    raw=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
                    c.execute('INSERT INTO auth_sessions(token_hash,user_id,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,?)',
                              (token_hash(raw),user['id'],token_hash(csrf),now,now+SESSION_SECONDS))
                    self._audit(c,user['id'],'login.succeeded',user['id'])
                    result=(raw,csrf,int(user['id']));outcome='ok'
        if outcome=='limited':raise RateLimited('Too many test attempts. Try again later.')
        if outcome!='ok':raise AuthenticationFailed('Invalid test credentials.')
        return result

    def authenticate(self,raw_token: str | None) -> Principal:
        if not isinstance(raw_token,str) or not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}',raw_token):
            raise AuthenticationFailed('Sign in with a fictional account.')
        hashed=token_hash(raw_token);now=self.now()
        with self.db.read() as c:
            row=c.execute('''SELECT u.id,u.email,u.display_name,u.role,u.enabled,
                s.token_hash,s.csrf_hash,s.expires_at,s.revoked FROM auth_sessions s
                JOIN users u ON u.id=s.user_id WHERE s.token_hash=?''',(hashed,)).fetchone()
            if not row or row['revoked'] or not row['enabled'] or int(row['expires_at'])<=now:
                raise AuthenticationFailed('Sign in with a fictional account.')
            return Principal(int(row['id']),row['email'],row['display_name'],row['role'],row['token_hash'],row['csrf_hash'],int(row['expires_at']))

    def csrf_ok(self,principal: Principal,header: str | None,cookie: str | None) -> bool:
        if not isinstance(header,str) or not isinstance(cookie,str):return False
        if not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}',header) or not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}',cookie):return False
        return hmac.compare_digest(header,cookie) and hmac.compare_digest(token_hash(header),principal.csrf_hash)

    def logout(self,principal: Principal) -> None:
        with self.db.tx() as c:
            c.execute('UPDATE auth_sessions SET revoked=1 WHERE token_hash=?',(principal.session_hash,))
            self._audit(c,principal.id,'logout',principal.id)

    def change_password(self,principal: Principal,old: str,new: str) -> None:
        validate_password(new);encoded=self.passwords.hash(new)
        with self.db.tx() as c:
            row=Engine._user(c,principal.id)
            if not self._verify(row['password_hash'],old):raise AuthenticationFailed('Current test password is incorrect.')
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(encoded,principal.id))
            c.execute('UPDATE auth_sessions SET revoked=1 WHERE user_id=?',(principal.id,))
            self._audit(c,principal.id,'password.changed',principal.id)

    def permissions(self,user_id: int) -> list[str]:
        with self.db.read() as c:
            user=Engine._user(c,user_id)
            if user['role']=='superuser':return sorted(self.grantable)
            if user['role'] not in ('admin','manager'):return []
            return [r['permission'] for r in c.execute('SELECT permission FROM staff_permissions WHERE user_id=? ORDER BY permission',(user_id,))]

    def require_staff_permission(self,user_id: int,permission: str) -> None:
        if permission not in self.grantable:raise PermissionDenied('Forbidden or unknown staff permission.')
        with self.db.read() as c:
            if not Engine._is_authorized(c,user_id,permission):raise PermissionDenied('This account does not have the required test permission.')

    def accept_test_notice(self,user_id: int) -> None:
        """A fictional notice event only—not legal consent or an approved workflow."""
        with self.db.tx() as c:
            if Engine._user(c,user_id)['role']!='player':raise PermissionDenied('Only a test player can acknowledge their own notice.')
            c.execute('INSERT OR IGNORE INTO test_notices(user_id,version,accepted_at) VALUES(?,?,?)',(user_id,TEST_NOTICE_VERSION,self.now()))
            self._audit(c,user_id,'fixture.notice_acknowledged',user_id)

    def test_players(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT id,display_name FROM users WHERE role='player' AND enabled=1 ORDER BY display_name,id")]

    def test_sessions(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute('''SELECT s.id,s.label,s.capacity,COUNT(r.user_id) AS occupied
                FROM openplay_sessions s LEFT JOIN session_registrations r ON r.session_id=s.id
                GROUP BY s.id ORDER BY s.id''')]

    def test_checkout(self,actor: int,session_id: int,players: list[int]) -> dict:
        if not players or len(players)>100 or len(players)!=len(set(players)) or actor not in players:
            raise ValueError('Include the signed-in test player once and select distinct test accounts.')
        with self.db.tx() as c:
            if Engine._user(c,actor)['role']!='player':raise PermissionDenied('Only a signed-in test player may self-book.')
            ses=c.execute('SELECT * FROM openplay_sessions WHERE id=?',(session_id,)).fetchone()
            if not ses:raise ValueError('Test session not found.')
            for uid in players:
                if Engine._user(c,uid)['role']!='player':raise ValueError('Every sample participant needs a player account.')
                if not c.execute('SELECT 1 FROM test_notices WHERE user_id=? AND version=?',(uid,TEST_NOTICE_VERSION)).fetchone():
                    raise Conflict('Each test participant must acknowledge their own fictional recording notice first.')
                if c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,uid)).fetchone():
                    raise Conflict('A test player is already registered in this session.')
            occupied=c.execute('SELECT COUNT(*) FROM session_registrations WHERE session_id=?',(session_id,)).fetchone()[0]
            if occupied+len(players)>ses['capacity']:raise Conflict('This sample session no longer has enough places.')
            price=c.execute('SELECT open_rate FROM auth_test_settings WHERE singleton=1').fetchone()[0];amount=int(price)*len(players)
            cur=c.execute("INSERT INTO auth_test_bookings(session_id,booker_id,amount,state,created_at) VALUES(?,?,?,'simulated_paid',?)",(session_id,actor,amount,self.now()))
            booking_id=int(cur.lastrowid)
            c.executemany('INSERT INTO auth_test_booking_players(booking_id,user_id) VALUES(?,?)',[(booking_id,p) for p in players])
            c.executemany("INSERT INTO session_registrations(session_id,user_id,payment_state) VALUES(?,?,'test_paid')",[(session_id,p) for p in players])
            self._audit(c,actor,'fixture.checkout',booking_id)
            return {'booking_id':booking_id,'amount':amount,'state':'simulated_paid'}

    def staff_bookings(self,user_id: int) -> list[dict]:
        self.require_staff_permission(user_id,'bookings.view')
        with self.db.read() as c:
            return [dict(r) for r in c.execute('SELECT id,session_id,booker_id,amount,state,created_at FROM auth_test_bookings ORDER BY id DESC LIMIT 100')]

    def set_test_open_rate(self,user_id: int,amount: int) -> None:
        if not isinstance(amount,int) or isinstance(amount,bool) or not 1<=amount<=100000:raise ValueError('Invalid test price.')
        with self.db.tx() as c:
            if not Engine._is_authorized(c,user_id,'settings.rates.edit'):raise PermissionDenied('Rate-edit permission required.')
            c.execute('UPDATE auth_test_settings SET open_rate=? WHERE singleton=1',(amount,))
            self._audit(c,user_id,'fixture.rate_changed')
