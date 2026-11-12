"""Internal, test-only booking -> queue -> match -> result domain prototype.

No real accounts, money, QR scans, network API or email delivery. This module is
not production-ready. All fixture email addresses must end in .invalid.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import hmac
import secrets
import sqlite3
from typing import Iterator


class DomainError(Exception):
    pass


class PermissionDenied(DomainError):
    pass


class Conflict(DomainError):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        raise ValueError("Test password must have 12 to 128 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        name, rounds, salt, digest = encoded.split("$")
        n = int(rounds)
        if name != "pbkdf2_sha256" or not 1 <= n <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), n).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError, AttributeError):
        return False


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 20000")
        return con

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            yield con
        finally:
            con.close()

    def _initialize(self) -> None:
        with self.read() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
              display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('player','superuser','admin','manager')),
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS staff_permissions (
              user_id INTEGER NOT NULL REFERENCES users(id), permission TEXT NOT NULL,
              PRIMARY KEY(user_id,permission)
            );
            CREATE TABLE IF NOT EXISTS openplay_sessions (
              id INTEGER PRIMARY KEY, label TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>=4),
              qr_token_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_registrations (
              session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              user_id INTEGER NOT NULL REFERENCES users(id),
              payment_state TEXT NOT NULL CHECK(payment_state IN ('test_paid')),
              PRIMARY KEY(session_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS checkins (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              user_id INTEGER NOT NULL REFERENCES users(id), checked_in_at TEXT NOT NULL,
              UNIQUE(session_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS queue_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              user_id INTEGER NOT NULL REFERENCES users(id),
              classification TEXT NOT NULL CHECK(classification IN ('initial','pending','winner','loser')),
              state TEXT NOT NULL CHECK(state IN ('waiting','playing','done')),
              source_match_id INTEGER, created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_queue_entry
              ON queue_entries(session_id,user_id) WHERE state IN ('waiting','playing');
            -- Optional manual order for a fictional waiting entry. The additive
            -- table preserves old demo DBs; absent overrides keep check-in FIFO.
            CREATE TABLE IF NOT EXISTS queue_positions (
              entry_id INTEGER PRIMARY KEY REFERENCES queue_entries(id) ON DELETE CASCADE,
              position INTEGER NOT NULL CHECK(position>0)
            );
            CREATE TABLE IF NOT EXISTS matches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              service TEXT NOT NULL CHECK(service IN ('openplay','court_rental')),
              session_id INTEGER REFERENCES openplay_sessions(id), court_id INTEGER NOT NULL CHECK(court_id IN (1,2)),
              status TEXT NOT NULL CHECK(status IN ('active','ended')),
              score_a INTEGER, score_b INTEGER, score_state TEXT NOT NULL DEFAULT 'missing'
                CHECK(score_state IN ('missing','pending','confirmed')),
              confirm_a_user INTEGER REFERENCES users(id), confirm_b_user INTEGER REFERENCES users(id),
              winning_team TEXT CHECK(winning_team IS NULL OR winning_team IN ('A','B')),
              created_at TEXT NOT NULL, ended_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_match_per_court ON matches(court_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS match_players (
              match_id INTEGER NOT NULL REFERENCES matches(id), user_id INTEGER NOT NULL REFERENCES users(id),
              team TEXT NOT NULL CHECK(team IN ('A','B')), PRIMARY KEY(match_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS recordings (
              id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL UNIQUE REFERENCES matches(id),
              court_id INTEGER NOT NULL CHECK(court_id IN (1,2)),
              state TEXT NOT NULL CHECK(state IN ('starting','recording','stopping','ready','failed','interrupted')),
              file_path TEXT, source TEXT NOT NULL CHECK(source='synthetic_test'),
              failure TEXT, started_at TEXT NOT NULL, ready_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_recording_per_court ON recordings(court_id)
              WHERE state IN ('starting','recording','stopping');
            CREATE TABLE IF NOT EXISTS test_recording_retention (
              recording_id INTEGER PRIMARY KEY REFERENCES recordings(id),
              expires_at TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('scheduled','deleting','deleted','delete_failed')),
              deleted_at TEXT, failure TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL REFERENCES matches(id),
              user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL CHECK(kind IN ('replay','result')),
              recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'test_only' CHECK(state='test_only'), created_at TEXT NOT NULL,
              UNIQUE(match_id,user_id,kind)
            );
            """)


PERMISSIONS = frozenset({'queue.manage','session.manage','score.enter','settings.manage'})


class Engine:
    """Internal API called by tests, not an HTTP service or secure production auth layer."""
    def __init__(self, db: Database):
        self.db = db

    def create_fixture_user(self, email: str, name: str, password: str, role: str = 'player') -> int:
        if not email.lower().endswith('.invalid') or '@' not in email or len(email) > 254:
            raise ValueError('Only fictional .invalid email addresses are accepted in this internal prototype.')
        if role not in {'player','superuser','admin','manager'} or not name.strip():
            raise ValueError('Invalid fixture user.')
        encoded = hash_password(password)
        with self.db.tx() as c:
            cur = c.execute('INSERT INTO users(email,display_name,password_hash,role) VALUES(?,?,?,?)',
                            (email.lower(),name.strip()[:80],encoded,role))
            return int(cur.lastrowid)

    @staticmethod
    def _user(c: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        u = c.execute('SELECT * FROM users WHERE id=? AND enabled=1',(user_id,)).fetchone()
        if not u: raise PermissionDenied('Unknown or disabled test user.')
        return u

    @staticmethod
    def _is_authorized(c: sqlite3.Connection, actor: int, permission: str) -> bool:
        u=Engine._user(c,actor)
        return u['role']=='superuser' or (u['role'] in ('admin','manager') and bool(c.execute(
            'SELECT 1 FROM staff_permissions WHERE user_id=? AND permission=?',(actor,permission)).fetchone()))

    def grant_staff_permission(self, actor: int, target: int, permission: str) -> None:
        if permission not in PERMISSIONS: raise ValueError('Unknown or forbidden permission.')
        with self.db.tx() as c:
            if self._user(c,actor)['role'] != 'superuser': raise PermissionDenied('Only Superuser grants permissions.')
            if self._user(c,target)['role'] not in ('admin','manager'): raise ValueError('Only Admin and Manager get staff permissions.')
            c.execute('INSERT OR IGNORE INTO staff_permissions(user_id,permission) VALUES(?,?)',(target,permission))

    def create_session(self, actor: int, label: str, venue_qr_token: str, capacity: int = 22) -> int:
        if not label or not venue_qr_token or capacity < 4 or capacity > 100: raise ValueError('Invalid test session.')
        token_hash=hashlib.sha256(venue_qr_token.encode()).hexdigest()
        with self.db.tx() as c:
            if not self._is_authorized(c,actor,'session.manage'): raise PermissionDenied('Session permission required.')
            cur=c.execute('INSERT INTO openplay_sessions(label,capacity,qr_token_hash) VALUES(?,?,?)',(label,capacity,token_hash))
            return int(cur.lastrowid)

    def fixture_test_payment(self, actor: int, session_id: int, player_id: int) -> None:
        """Create an explicitly fake paid registration; never call a payment provider."""
        with self.db.tx() as c:
            if self._user(c,actor)['role']!='superuser': raise PermissionDenied('Test fixture creation requires Superuser.')
            if self._user(c,player_id)['role']!='player': raise ValueError('Fixture participant must have player role.')
            sess=c.execute('SELECT capacity FROM openplay_sessions WHERE id=?',(session_id,)).fetchone()
            if not sess: raise ValueError('Session not found.')
            n=c.execute('SELECT COUNT(*) FROM session_registrations WHERE session_id=?',(session_id,)).fetchone()[0]
            if n>=sess['capacity']: raise Conflict('Test session is full.')
            c.execute("INSERT INTO session_registrations(session_id,user_id,payment_state) VALUES(?,?,'test_paid')",(session_id,player_id))

    def check_in(self, session_id: int, player_id: int, venue_qr_token: str) -> int:
        """A token stands in for a venue QR; scanning and production token security are not implemented."""
        digest=hashlib.sha256(venue_qr_token.encode()).hexdigest()
        with self.db.tx() as c:
            player=self._user(c,player_id)
            s=c.execute('SELECT * FROM openplay_sessions WHERE id=?',(session_id,)).fetchone()
            if player['role']!='player' or not s or not hmac.compare_digest(s['qr_token_hash'],digest):
                raise PermissionDenied('Invalid test venue QR or player.')
            if not c.execute("SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=? AND payment_state='test_paid'",(session_id,player_id)).fetchone():
                raise PermissionDenied('A confirmed test registration is required.')
            existing=c.execute('SELECT id FROM checkins WHERE session_id=? AND user_id=?',(session_id,player_id)).fetchone()
            if existing: return int(existing['id'])
            cur=c.execute('INSERT INTO checkins(session_id,user_id,checked_in_at) VALUES(?,?,?)',(session_id,player_id,timestamp()))
            c.execute("INSERT INTO queue_entries(session_id,user_id,classification,state,created_at) VALUES(?,?,'initial','waiting',?)",(session_id,player_id,timestamp()))
            return int(cur.lastrowid)

    def queue(self, session_id: int) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("""SELECT q.id,q.user_id,q.classification,q.state,q.source_match_id,u.display_name
                                                  FROM queue_entries q JOIN users u ON u.id=q.user_id
                                                  LEFT JOIN queue_positions pos ON pos.entry_id=q.id
                                                  WHERE q.session_id=? AND q.state='waiting'
                                                  ORDER BY COALESCE(pos.position,q.id),q.id""",(session_id,))]

    def assign_next(self, actor: int, session_id: int, court_id: int) -> int:
        """Provisional internal matching: winners-only or losers-only when 4 exist,
        otherwise mix FIFO. Pending/initial remain eligible. Actual tie-breaking,
        partner-preservation rules and pending-result classification require approval.
        """
        if court_id not in (1,2): raise ValueError('Court must be 1 or 2.')
        with self.db.tx() as c:
            if not self._is_authorized(c,actor,'queue.manage'): raise PermissionDenied('Queue permission required.')
            if c.execute("SELECT 1 FROM matches WHERE court_id=? AND status='active'",(court_id,)).fetchone():
                raise Conflict('Court already has an active match.')
            # A player can have a waiting entry in another session, but cannot
            # physically be assigned to two simultaneous active matches.
            waiting=list(c.execute("""SELECT q.* FROM queue_entries q
                LEFT JOIN queue_positions pos ON pos.entry_id=q.id
                WHERE q.session_id=? AND q.state='waiting' AND NOT EXISTS (
                  SELECT 1 FROM match_players p JOIN matches m ON m.id=p.match_id
                  WHERE p.user_id=q.user_id AND m.status='active'
                ) ORDER BY COALESCE(pos.position,q.id),q.id""",(session_id,)))
            if len(waiting)<4: raise Conflict('At least four queued players are required.')
            groups=[]
            for category in ('winner','loser'):
                group=[x for x in waiting if x['classification']==category]
                if len(group)>=4: groups.append(group[:4])
            # A staff override changes priority within this provisional grouping;
            # compare the visible order, not the immutable original entry ID.
            priority={entry['id']:i for i,entry in enumerate(waiting)}
            chosen=min(groups,key=lambda g:priority[g[0]['id']]) if groups else waiting[:4]
            cur=c.execute("INSERT INTO matches(service,session_id,court_id,status,created_at) VALUES('openplay',?,?,'active',?)",(session_id,court_id,timestamp()))
            mid=int(cur.lastrowid)
            for idx,entry in enumerate(chosen):
                c.execute('INSERT INTO match_players(match_id,user_id,team) VALUES(?,?,?)',(mid,entry['user_id'],'A' if idx<2 else 'B'))
                c.execute("UPDATE queue_entries SET state='playing' WHERE id=?",(entry['id'],))
            return mid

    def assign_selected(self, actor: int, session_id: int, court_id: int,
                        team_a: list[int], team_b: list[int]) -> int:
        """Fictional staff override: explicitly choose waiting players and partners.

        This is not a real staff authentication system or a complete reorder UI.
        SQLite serializes the selection so no participant is assigned twice.
        """
        players=[*team_a,*team_b]
        if (court_id not in (1,2) or len(team_a)!=2 or len(team_b)!=2
            or len(set(players))!=4 or not all(isinstance(p,int) and not isinstance(p,bool) for p in players)):
            raise ValueError('Choose two distinct test players for each team.')
        with self.db.tx() as c:
            if not self._is_authorized(c,actor,'queue.manage'):raise PermissionDenied('Queue permission required.')
            if c.execute("SELECT 1 FROM matches WHERE court_id=? AND status='active'",(court_id,)).fetchone():
                raise Conflict('Court already has an active match.')
            entries=[]
            for p in players:
                q=c.execute("""SELECT q.id,q.user_id FROM queue_entries q
                    WHERE q.session_id=? AND q.user_id=? AND q.state='waiting' AND NOT EXISTS (
                      SELECT 1 FROM match_players mp JOIN matches m ON m.id=mp.match_id
                      WHERE mp.user_id=q.user_id AND m.status='active')""",(session_id,p)).fetchone()
                if not q:raise Conflict('Every selected sample player must be waiting and not in another active match.')
                entries.append(q)
            mid=int(c.execute("INSERT INTO matches(service,session_id,court_id,status,created_at) VALUES('openplay',?,?,'active',?)",
                              (session_id,court_id,timestamp())).lastrowid)
            for i,q in enumerate(entries):
                c.execute('INSERT INTO match_players(match_id,user_id,team) VALUES(?,?,?)',(mid,q['user_id'],'A' if i<2 else 'B'))
                c.execute("UPDATE queue_entries SET state='playing' WHERE id=?",(q['id'],))
            return mid

    def create_test_rental_match(self, actor: int, court_id: int, players: list[int]) -> int:
        """Internal fixture only: rental booking/account association is not yet implemented."""
        if court_id not in (1,2) or len(players)!=4 or len(set(players))!=4: raise ValueError('Use four distinct test players.')
        with self.db.tx() as c:
            if self._user(c,actor)['role']!='superuser': raise PermissionDenied('Test fixture creation requires Superuser.')
            if c.execute("SELECT 1 FROM matches WHERE court_id=? AND status='active'",(court_id,)).fetchone(): raise Conflict('Court occupied.')
            for p in players:
                if self._user(c,p)['role']!='player': raise ValueError('Rental fixture participants must be players.')
                if c.execute("""SELECT 1 FROM match_players mp JOIN matches m ON m.id=mp.match_id
                                WHERE mp.user_id=? AND m.status='active'""",(p,)).fetchone():
                    raise Conflict('A test player is already in an active match.')
            cur=c.execute("INSERT INTO matches(service,court_id,status,created_at) VALUES('court_rental',?,'active',?)",(court_id,timestamp()))
            mid=int(cur.lastrowid)
            for i,p in enumerate(players): c.execute('INSERT INTO match_players(match_id,user_id,team) VALUES(?,?,?)',(mid,p,'A' if i<2 else 'B'))
            return mid

    def _may_operate_match(self, c: sqlite3.Connection, actor: int, match_id: int, staff_permission: str) -> bool:
        u=self._user(c,actor)
        if u['role']=='superuser': return True
        if u['role']=='player' and c.execute('SELECT 1 FROM match_players WHERE match_id=? AND user_id=?',(match_id,actor)).fetchone(): return True
        return self._is_authorized(c,actor,staff_permission)

    def end_match(self, actor: int, match_id: int) -> None:
        """Return Open Play players to queue even when scores are absent or pending.
        Ending a match does not automatically stop its recording; a later start
        on that court may trigger rollover as the user requested.
        """
        with self.db.tx() as c:
            m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not m: raise ValueError('Match not found.')
            if not self._may_operate_match(c,actor,match_id,'queue.manage'): raise PermissionDenied('Only a participant or authorized staff may end this test match.')
            if m['status']=='ended': return
            c.execute("UPDATE matches SET status='ended',ended_at=? WHERE id=?",(timestamp(),match_id))
            if m['service']=='openplay':
                players=list(c.execute('SELECT user_id FROM match_players WHERE match_id=? ORDER BY user_id',(match_id,)))
                for p in players:
                    c.execute("UPDATE queue_entries SET state='done' WHERE session_id=? AND user_id=? AND state='playing'",(m['session_id'],p['user_id']))
                    c.execute("INSERT INTO queue_entries(session_id,user_id,classification,state,source_match_id,created_at) VALUES(?,?,'pending','waiting',?,?)",(m['session_id'],p['user_id'],match_id,timestamp()))

    def submit_score(self, actor: int, match_id: int, a: int, b: int) -> None:
        if not all(isinstance(x,int) and not isinstance(x,bool) and 0<=x<=99 for x in (a,b)):
            raise ValueError('Scores must be whole numbers from 0 to 99 in this test.')
        high,low=max(a,b),min(a,b)
        # A game ends at the first point where the winning team has at least
        # eleven and leads by two. For example 11-9 and 12-10 are valid;
        # 12-9 cannot be a final score because the game ended at 11-9.
        if high<11 or high-low<2 or (high>11 and high-low!=2):
            raise ValueError('Open Play test score must end at 11, win by 2.')
        with self.db.tx() as c:
            m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not m or m['service']!='openplay' or m['status']!='ended': raise Conflict('An ended Open Play match is required.')
            if not self._may_operate_match(c,actor,match_id,'score.enter'): raise PermissionDenied('Score-entry permission required.')
            if m['score_state']=='confirmed': raise Conflict('Confirmed scores require a dispute/correction process, not implemented.')
            c.execute("UPDATE matches SET score_a=?,score_b=?,score_state='pending',confirm_a_user=NULL,confirm_b_user=NULL,winning_team=NULL WHERE id=?",(a,b,match_id))

    @staticmethod
    def _enqueue(c: sqlite3.Connection, match_id: int, kind: str) -> None:
        m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
        if not m: raise ValueError('Match not found.')
        # Rental email recipients/delivery remain undecided; do not guess in this prototype.
        if m['service']!='openplay': return
        players=list(c.execute('SELECT u.id,u.email FROM users u JOIN match_players p ON p.user_id=u.id WHERE p.match_id=?',(match_id,)))
        for p in players:
            if kind=='replay':
                subject='[TEST ONLY] Your private replay is ready'
                body=f'TEST ONLY: Sign in and open match {match_id} in the test portal. Score: {m["score_state"]}. No email or real replay link was sent.'
            else:
                subject='[TEST ONLY] Your match result is confirmed'
                body=f'TEST ONLY: Team A {m["score_a"]} - {m["score_b"]} Team B. Both teams confirmed; fictional Open Play wins and win rate are updated in Rankings. No email was actually sent.'
            c.execute('INSERT OR IGNORE INTO outbox(match_id,user_id,kind,recipient,subject,body,created_at) VALUES(?,?,?,?,?,?,?)',
                      (match_id,p['id'],kind,p['email'],subject,body,timestamp()))

    def confirm_score(self, actor: int, match_id: int) -> bool:
        with self.db.tx() as c:
            self._user(c,actor)
            mp=c.execute('SELECT team FROM match_players WHERE match_id=? AND user_id=?',(match_id,actor)).fetchone()
            m=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not mp: raise PermissionDenied('Only a player representing a team may confirm its score.')
            if not m or m['score_state']=='missing': raise Conflict('No score is awaiting confirmation.')
            if m['score_state']=='confirmed': return True
            col='confirm_a_user' if mp['team']=='A' else 'confirm_b_user'
            c.execute(f'UPDATE matches SET {col}=? WHERE id=?',(actor,match_id))
            current=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if current['confirm_a_user'] is None or current['confirm_b_user'] is None: return False
            winner='A' if current['score_a']>current['score_b'] else 'B'
            c.execute("UPDATE matches SET score_state='confirmed',winning_team=? WHERE id=?",(winner,match_id))
            for p in c.execute('SELECT user_id,team FROM match_players WHERE match_id=?',(match_id,)):
                category='winner' if p['team']==winner else 'loser'
                # Don't rewrite a player's later match or in-progress queue entry.
                c.execute("UPDATE queue_entries SET classification=? WHERE source_match_id=? AND user_id=? AND state='waiting'",(category,match_id,p['user_id']))
            self._enqueue(c,match_id,'result')
            return True

    def ranking(self) -> list[dict]:
        """Aggregate only. Final sorting/tie-breaking is not decided."""
        with self.db.read() as c:
            rows=c.execute("""SELECT p.user_id,u.display_name,
                         SUM(CASE WHEN p.team=m.winning_team THEN 1 ELSE 0 END) wins,
                         SUM(CASE WHEN p.team<>m.winning_team THEN 1 ELSE 0 END) losses
                         FROM match_players p JOIN matches m ON m.id=p.match_id JOIN users u ON u.id=p.user_id
                         WHERE m.service='openplay' AND m.score_state='confirmed'
                         GROUP BY p.user_id,u.display_name ORDER BY p.user_id""").fetchall()
            return [{**dict(r),'win_rate':round(100*r['wins']/(r['wins']+r['losses']),2)} for r in rows]

    def match(self, match_id: int) -> dict:
        with self.db.read() as c:
            row=c.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
            if not row: raise ValueError('Match not found.')
            result=dict(row); result['players']=[dict(p) for p in c.execute('SELECT user_id,team FROM match_players WHERE match_id=? ORDER BY team,user_id',(match_id,))]
            return result

    def private_recording(self, actor: int, match_id: int) -> Path:
        with self.db.read() as c:
            user=self._user(c,actor)
            participant=c.execute('SELECT 1 FROM match_players WHERE match_id=? AND user_id=?',(match_id,actor)).fetchone()
            if user['role']!='superuser' and not (user['role']=='player' and participant):
                raise PermissionDenied('Only a match participant or Superuser may view this recording.')
            r=c.execute("""SELECT r.file_path,t.expires_at,t.state AS retention_state
                FROM recordings r LEFT JOIN test_recording_retention t ON t.recording_id=r.id
                WHERE r.match_id=? AND r.state='ready'""",(match_id,)).fetchone()
            if not r or not r['file_path']: raise Conflict('No ready recording.')
            if r['expires_at']:
                try: expiry=datetime.fromisoformat(r['expires_at'])
                except (TypeError,ValueError): raise Conflict('Invalid test retention; replay unavailable.')
                if expiry.tzinfo is None or r['retention_state']!='scheduled' or datetime.now(timezone.utc)>=expiry:
                    raise Conflict('Test recording has expired or is unavailable.')
            return Path(r['file_path'])

    def outbox(self, match_id: int, kind: str | None = None) -> list[dict]:
        with self.db.read() as c:
            sql='SELECT * FROM outbox WHERE match_id=?'+(' AND kind=?' if kind else '')+' ORDER BY id'
            return [dict(r) for r in c.execute(sql,(match_id,kind) if kind else (match_id,))]
