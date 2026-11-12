"""One-database, fictional-only booking and operations integration prototype.

No provider, real money, reservation, email or camera. SQL transactions protect
fictional concurrency tests; production payment reconciliation is NOT implemented.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo
import hashlib
import time

from lg_sports_deck.accounts.service import Accounts, TEST_NOTICE_VERSION
from lg_sports_deck.core.domain import Conflict, Database, Engine, PermissionDenied, timestamp
from .notifications import install_test_schema

MANILA=ZoneInfo('Asia/Manila')
EXTRA_GRANTABLE=frozenset({'session.manage','attendance.noshow','attendance.qr.display','settings.limits.edit','sales.view','payments.manual_verify','schedule.manage'})


class UnifiedDemo:
    def __init__(self,db: Database,accounts: Accounts,clock: Callable[[],float]=time.time):
        self.db=db;self.accounts=accounts;self.clock=clock
        with db.read() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS unified_settings (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              open_rate INTEGER NOT NULL, court_rate INTEGER NOT NULL, paddle_rate INTEGER NOT NULL,
              open_limit INTEGER NOT NULL, court_limit INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO unified_settings VALUES(1,200,250,50,22,12);
            CREATE TABLE IF NOT EXISTS unified_sessions (
              session_id INTEGER PRIMARY KEY REFERENCES openplay_sessions(id),
              day TEXT NOT NULL, start_min INTEGER NOT NULL, end_min INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS unified_sessions_day ON unified_sessions(day,start_min,end_min);
            CREATE TABLE IF NOT EXISTS unified_bookings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL CHECK(kind IN ('openplay','court')),
              day TEXT NOT NULL, start_min INTEGER NOT NULL, end_min INTEGER NOT NULL,
              court_id INTEGER CHECK(court_id IN (1,2)), session_id INTEGER REFERENCES openplay_sessions(id),
              booker_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL,
              state TEXT NOT NULL CHECK(state='simulated_paid'),
              payment_method TEXT NOT NULL CHECK(payment_method='simulated_gcash'),
              created_at INTEGER NOT NULL, created_day TEXT NOT NULL,
              CHECK((kind='openplay' AND session_id IS NOT NULL AND court_id IS NULL) OR
                    (kind='court' AND session_id IS NULL AND court_id IS NOT NULL))
            );
            CREATE INDEX IF NOT EXISTS unified_booking_schedule ON unified_bookings(day,court_id,start_min,end_min);
            CREATE TABLE IF NOT EXISTS unified_booking_players (
              booking_id INTEGER NOT NULL REFERENCES unified_bookings(id),
              user_id INTEGER NOT NULL REFERENCES users(id),
              attendance TEXT NOT NULL DEFAULT 'booked' CHECK(attendance IN ('booked','no_show')),
              PRIMARY KEY(booking_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS unified_waitlist (
              session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              user_id INTEGER NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
              PRIMARY KEY(session_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS unified_notifications (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
              session_id INTEGER NOT NULL REFERENCES openplay_sessions(id),
              kind TEXT NOT NULL CHECK(kind='place_opened'), message TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS unified_rental_matches (
              match_id INTEGER PRIMARY KEY REFERENCES matches(id),
              booking_id INTEGER NOT NULL REFERENCES unified_bookings(id)
            );
            ''')
        install_test_schema(db)

    def now(self) -> datetime:return datetime.fromtimestamp(self.clock(),MANILA)
    def _audit(self,c,actor,event,target=None):self.accounts._audit(c,actor,event,target)

    def _day(self,day: date) -> str:
        today=self.now().date()
        if not isinstance(day,date) or isinstance(day,datetime) or not today<=day<=today+timedelta(days=366):
            raise ValueError('Choose a fictional test date from today through the next 366 days.')
        return day.isoformat()

    def _session_not_finished(self,day_str: str,end_min: int):
        now=self.now()
        if day_str==now.date().isoformat() and end_min<=now.hour*60+now.minute:
            raise Conflict('This fictional session has already ended.')

    @staticmethod
    def _interval(start_hour: int,duration: int) -> tuple[int,int]:
        if (not isinstance(start_hour,int) or isinstance(start_hour,bool) or
            not isinstance(duration,int) or isinstance(duration,bool)):
            raise ValueError('Use whole test hours.')
        start=start_hour*60;end=start+duration*60
        if start<360 or start>=1440 or duration<1 or end>1440:
            raise ValueError('Use whole hours between 6 AM and midnight.')
        return start,end

    @staticmethod
    def _require(c,actor: int,permission: str):
        if not Engine._is_authorized(c,actor,permission):raise PermissionDenied('This fictional account lacks the required action permission.')

    @staticmethod
    def _user_player(c,actor: int):
        u=Engine._user(c,actor)
        if u['role']!='player':raise PermissionDenied('A fictional player account is required.')
        return u

    @staticmethod
    def _players(c,actor: int,players: list[int],maximum: int):
        if (not players or len(players)>maximum or len(players)!=len(set(players)) or actor not in players
            or any(not isinstance(p,int) or isinstance(p,bool) or p<1 for p in players)):
            raise ValueError('Include the signed-in player once and choose distinct fictional players within the group limit.')
        for player in players:
            if Engine._user(c,player)['role']!='player':raise ValueError('All test participants need their own fictional player account.')
            if not c.execute('SELECT 1 FROM test_notices WHERE user_id=? AND version=?',(player,TEST_NOTICE_VERSION)).fetchone():
                raise Conflict('Each fictional participant must acknowledge their own recording notice first.')

    @staticmethod
    def _court_conflict(c,day: str,court: int,start: int,end: int) -> bool:
        if c.execute('SELECT 1 FROM unified_sessions WHERE day=? AND start_min<? AND end_min>? LIMIT 1',(day,end,start)).fetchone():return True
        return bool(c.execute("""SELECT 1 FROM unified_bookings WHERE kind='court' AND day=? AND court_id=?
                AND start_min<? AND end_min>? LIMIT 1""",(day,court,end,start)).fetchone())

    def catalog(self,actor: int) -> dict:
        with self.db.read() as c:
            user=Engine._user(c,actor);settings=dict(c.execute('SELECT * FROM unified_settings WHERE singleton=1').fetchone());settings.pop('singleton')
            sessions=[]
            can_view_sessions=(user['role'] in ('player','superuser') or Engine._is_authorized(c,actor,'session.view'))
            for row in c.execute('''SELECT s.id,s.label,s.capacity,t.day,t.start_min,t.end_min,
                (SELECT COUNT(*) FROM session_registrations r WHERE r.session_id=s.id) AS occupied,
                (SELECT COUNT(*) FROM unified_waitlist w WHERE w.session_id=s.id) AS waiting
                FROM openplay_sessions s JOIN unified_sessions t ON t.session_id=s.id
                ORDER BY t.day,t.start_min,s.id'''):
                if not can_view_sessions:continue
                r=dict(row);r['remaining']=max(0,r['capacity']-r['occupied']);r['full']=r['remaining']==0
                now=self.now();r['ended']=r['day']<now.date().isoformat() or (r['day']==now.date().isoformat() and r['end_min']<=now.hour*60+now.minute)
                r['on_waitlist']=bool(user['role']=='player' and c.execute('SELECT 1 FROM unified_waitlist WHERE session_id=? AND user_id=?',(r['id'],actor)).fetchone())
                r['registered']=bool(user['role']=='player' and c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(r['id'],actor)).fetchone())
                sessions.append(r)
            people=[]
            if user['role']=='player' or user['role']=='superuser' or Engine._is_authorized(c,actor,'bookings.view'):
                people=[dict(r) for r in c.execute("SELECT id,display_name FROM users WHERE role='player' AND enabled=1 ORDER BY display_name,id")]
            return {'settings':settings,'sessions':sessions,'players':people,'today':self.now().date().isoformat(),'mode':'fictional-only'}

    def create_session(self,actor: int,day: date,start_hour: int,duration: int,label: str,venue_token: str) -> int:
        day_str=self._day(day);start,end=self._interval(start_hour,duration)
        self._session_not_finished(day_str,end)
        if duration not in (3,4):raise ValueError('A fictional Open Play session is three or four hours.')
        if not isinstance(label,str) or not label.strip() or len(label)>80:raise ValueError('Use a fictional session label.')
        if not isinstance(venue_token,str) or not 6<=len(venue_token)<=256:raise ValueError('Use a fictional venue token of 6–256 characters.')
        try:digest=hashlib.sha256(venue_token.encode('utf-8')).hexdigest();label.strip().encode('utf-8')
        except UnicodeError:raise ValueError('Invalid fictional session details.')
        with self.db.tx() as c:
            self._require(c,actor,'session.manage')
            if self._court_conflict(c,day_str,1,start,end) or self._court_conflict(c,day_str,2,start,end):
                raise Conflict('Both courts must be free for this fictional Open Play session.')
            cap=c.execute('SELECT open_limit FROM unified_settings WHERE singleton=1').fetchone()[0]
            cur=c.execute('INSERT INTO openplay_sessions(label,capacity,qr_token_hash) VALUES(?,?,?)',(label.strip(),cap,digest))
            sid=int(cur.lastrowid)
            c.execute('INSERT INTO unified_sessions VALUES(?,?,?,?)',(sid,day_str,start,end))
            self._audit(c,actor,'unified.session_created',sid)
            return sid

    def set_rates(self,actor: int,open_rate: int,court_rate: int,paddle_rate: int):
        rates=(open_rate,court_rate,paddle_rate)
        if any(not isinstance(x,int) or isinstance(x,bool) or not 1<=x<=100000 for x in rates):raise ValueError('Invalid fictional rates.')
        with self.db.tx() as c:
            self._require(c,actor,'settings.rates.edit')
            c.execute('UPDATE unified_settings SET open_rate=?,court_rate=?,paddle_rate=? WHERE singleton=1',rates)
            self._audit(c,actor,'unified.rates_changed')

    def set_limits(self,actor: int,open_limit: int,court_limit: int):
        if (not isinstance(open_limit,int) or isinstance(open_limit,bool) or not 4<=open_limit<=100 or
            not isinstance(court_limit,int) or isinstance(court_limit,bool) or not 1<=court_limit<=50):raise ValueError('Invalid fictional limits.')
        with self.db.tx() as c:
            self._require(c,actor,'settings.limits.edit')
            c.execute('UPDATE unified_settings SET open_limit=?,court_limit=? WHERE singleton=1',(open_limit,court_limit))
            self._audit(c,actor,'unified.limits_changed')

    def _quote_openplay_in_tx(self,c,actor: int,session_id: int,players: list[int]):
        """Validate and price a fictional request under the caller's DB transaction.

        A quote does not reserve places or represent a payment. Recheck every
        rule under the committing transaction: user/consent, capacity, waitlist,
        date and current price may change after a quote was created.
        """
        self._user_player(c,actor)
        session=c.execute('''SELECT s.*,t.day,t.start_min,t.end_min FROM openplay_sessions s
            JOIN unified_sessions t ON t.session_id=s.id WHERE s.id=?''',(session_id,)).fetchone()
        if not session:raise ValueError('Fictional session not found.')
        self._day(date.fromisoformat(session['day']));self._session_not_finished(session['day'],session['end_min'])
        self._players(c,actor,players,session['capacity'])
        for player in players:
            if c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,player)).fetchone():
                raise Conflict('A fictional player is already booked in this session.')
        used=c.execute('SELECT COUNT(*) FROM session_registrations WHERE session_id=?',(session_id,)).fetchone()[0]
        if used+len(players)>session['capacity']:raise Conflict('This fictional session no longer has enough places.')
        waiting=c.execute('''SELECT 1 FROM unified_waitlist w JOIN users u ON u.id=w.user_id
            WHERE w.session_id=? AND u.enabled=1 LIMIT 1''',(session_id,)).fetchone()
        if waiting:
            for player in players:
                if not c.execute('SELECT 1 FROM unified_waitlist WHERE session_id=? AND user_id=?',(session_id,player)).fetchone():
                    raise Conflict('For this test, reopened places go first to existing fictional waiting-list members.')
        rate=c.execute('SELECT open_rate FROM unified_settings WHERE singleton=1').fetchone()[0]
        return session,int(rate)*len(players)

    def _book_openplay_in_tx(self,c,actor: int,session_id: int,players: list[int],*,expected_amount: int|None=None) -> dict:
        session,amount=self._quote_openplay_in_tx(c,actor,session_id,players)
        # This laboratory refuses to silently change a paid test quote. The
        # real price-lock/late-payment policy has not yet been approved.
        if expected_amount is not None and amount!=expected_amount:
            raise Conflict('The fictional quoted amount changed; this simulated payment needs review.')
        now=self.now()
        cur=c.execute('''INSERT INTO unified_bookings(kind,day,start_min,end_min,court_id,session_id,booker_id,amount,state,payment_method,created_at,created_day)
            VALUES('openplay',?,?,?,?,?,?,?,'simulated_paid','simulated_gcash',?,?)''',
            (session['day'],session['start_min'],session['end_min'],None,session_id,actor,amount,int(self.clock()),now.date().isoformat()))
        bid=int(cur.lastrowid)
        c.executemany('INSERT INTO unified_booking_players(booking_id,user_id) VALUES(?,?)',[(bid,p) for p in players])
        c.executemany("INSERT INTO session_registrations(session_id,user_id,payment_state) VALUES(?,?,'test_paid')",[(session_id,p) for p in players])
        c.executemany('DELETE FROM unified_waitlist WHERE session_id=? AND user_id=?',[(session_id,p) for p in players])
        self._audit(c,actor,'unified.openplay_checkout',bid)
        return {'booking_id':bid,'reference':f'TEST-{bid:05d}','amount':amount,'state':'simulated_paid'}

    def book_openplay(self,actor: int,session_id: int,players: list[int]) -> dict:
        # Existing one-click fictional simulator; no real gateway is involved.
        with self.db.tx() as c:
            return self._book_openplay_in_tx(c,actor,session_id,players)

    def _quote_court_in_tx(self,c,actor: int,day: date,start_hour: int,duration: int,court: int,players: list[int]):
        """Validate a fictional court quote without reserving its time."""
        day_str=self._day(day);start,end=self._interval(start_hour,duration)
        now=self.now()
        if day_str==now.date().isoformat() and start<now.hour*60+now.minute:
            raise Conflict('This fictional rental start time has already passed.')
        if court not in (1,2) or isinstance(court,bool):raise ValueError('Choose fictional court 1 or 2.')
        self._user_player(c,actor);settings=c.execute('SELECT * FROM unified_settings WHERE singleton=1').fetchone()
        self._players(c,actor,players,int(settings['court_limit']))
        if self._court_conflict(c,day_str,court,start,end):raise Conflict('This fictional court overlaps an Open Play session or another test rental.')
        return day_str,start,end,int(settings['court_rate'])*duration

    def _book_court_in_tx(self,c,actor: int,day: date,start_hour: int,duration: int,court: int,players: list[int],*,expected_amount: int|None=None) -> dict:
        day_str,start,end,amount=self._quote_court_in_tx(c,actor,day,start_hour,duration,court,players)
        if expected_amount is not None and amount!=expected_amount:
            raise Conflict('The fictional quoted amount changed; this simulated payment needs review.')
        now=self.now()
        cur=c.execute('''INSERT INTO unified_bookings(kind,day,start_min,end_min,court_id,session_id,booker_id,amount,state,payment_method,created_at,created_day)
            VALUES('court',?,?,?,?,NULL,?,?,'simulated_paid','simulated_gcash',?,?)''',
            (day_str,start,end,court,actor,amount,int(self.clock()),now.date().isoformat()))
        bid=int(cur.lastrowid)
        c.executemany('INSERT INTO unified_booking_players(booking_id,user_id) VALUES(?,?)',[(bid,p) for p in players])
        self._audit(c,actor,'unified.court_checkout',bid)
        return {'booking_id':bid,'reference':f'TEST-{bid:05d}','amount':amount,'state':'simulated_paid'}

    def book_court(self,actor: int,day: date,start_hour: int,duration: int,court: int,players: list[int]) -> dict:
        # Existing one-click fictional simulator; no real gateway is involved.
        with self.db.tx() as c:
            return self._book_court_in_tx(c,actor,day,start_hour,duration,court,players)

    def join_waitlist(self,actor: int,session_id: int):
        with self.db.tx() as c:
            self._user_player(c,actor)
            session=c.execute('''SELECT s.*,t.day,t.end_min FROM openplay_sessions s JOIN unified_sessions t ON t.session_id=s.id
                WHERE s.id=?''',(session_id,)).fetchone()
            if not session:raise ValueError('Fictional session not found.')
            self._day(date.fromisoformat(session['day']));self._session_not_finished(session['day'],session['end_min'])
            n=c.execute('SELECT COUNT(*) FROM session_registrations WHERE session_id=?',(session_id,)).fetchone()[0]
            if n<session['capacity']:raise Conflict('This fictional session is not full.')
            if c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,actor)).fetchone():
                raise Conflict('This fictional player is already booked.')
            c.execute('INSERT OR IGNORE INTO unified_waitlist VALUES(?,?,?)',(session_id,actor,int(self.clock())))
            self._audit(c,actor,'unified.waitlisted',session_id)

    def mark_no_show(self,actor: int,session_id: int,player_id: int) -> int:
        with self.db.tx() as c:
            self._require(c,actor,'attendance.noshow')
            reg=c.execute('''SELECT b.id,t.day,t.end_min FROM unified_bookings b JOIN unified_booking_players p ON p.booking_id=b.id
                JOIN unified_sessions t ON t.session_id=b.session_id
                WHERE b.session_id=? AND p.user_id=? AND p.attendance='booked' ''',(session_id,player_id)).fetchone()
            if not reg or not c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,player_id)).fetchone():
                raise Conflict('This fictional player has no active registration in the test session.')
            self._day(date.fromisoformat(reg['day']));self._session_not_finished(reg['day'],reg['end_min'])
            if c.execute('SELECT 1 FROM checkins WHERE session_id=? AND user_id=?',(session_id,player_id)).fetchone():
                raise Conflict('A checked-in fictional player cannot be marked as a no-show in this prototype.')
            c.execute("UPDATE unified_booking_players SET attendance='no_show' WHERE booking_id=? AND user_id=?",(reg['id'],player_id))
            c.execute('DELETE FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,player_id))
            waiting=[r['user_id'] for r in c.execute('SELECT user_id FROM unified_waitlist WHERE session_id=?',(session_id,))]
            msg='TEST ONLY: A place opened. First successful simulated payment by a waiting-list member gets it. No email was sent.'
            c.executemany("INSERT INTO unified_notifications(user_id,session_id,kind,message,created_at) VALUES(?,?,'place_opened',?,?)",
                          [(p,session_id,msg,int(self.clock())) for p in waiting])
            self._audit(c,actor,'unified.no_show',player_id)
            return len(waiting)

    @staticmethod
    def _booking_rows(c,sql: str,args: tuple) -> list[dict]:
        result=[]
        for row in c.execute(sql,args):
            item=dict(row);item['reference']=f"TEST-{item['id']:05d}"
            item['players']=[dict(r) for r in c.execute('''SELECT u.id,u.display_name,p.attendance FROM unified_booking_players p
                JOIN users u ON u.id=p.user_id WHERE p.booking_id=? ORDER BY u.id''',(item['id'],))]
            result.append(item)
        return result

    def my_bookings(self,actor: int) -> list[dict]:
        with self.db.read() as c:
            self._user_player(c,actor)
            return self._booking_rows(c,'''SELECT b.* FROM unified_bookings b JOIN unified_booking_players p ON p.booking_id=b.id
                WHERE p.user_id=? ORDER BY b.id DESC LIMIT 100''',(actor,))

    def staff_bookings(self,actor: int) -> list[dict]:
        with self.db.read() as c:
            self._require(c,actor,'bookings.view')
            return self._booking_rows(c,'SELECT * FROM unified_bookings ORDER BY id DESC LIMIT 100',())

    def roster(self,actor: int,session_id: int) -> list[dict]:
        with self.db.read() as c:
            if not (Engine._is_authorized(c,actor,'bookings.view') or Engine._is_authorized(c,actor,'attendance.noshow')):
                raise PermissionDenied('Booking-view or no-show permission required.')
            return [dict(r) for r in c.execute('''SELECT u.id,u.display_name,p.attendance,
                EXISTS(SELECT 1 FROM checkins ch WHERE ch.user_id=u.id AND ch.session_id=b.session_id) AS checked_in
                FROM unified_bookings b JOIN unified_booking_players p ON p.booking_id=b.id JOIN users u ON u.id=p.user_id
                WHERE b.session_id=? ORDER BY b.id,u.id''',(session_id,))]

    def sales(self,actor: int,period: str) -> dict:
        if period not in ('daily','monthly'):raise ValueError('Choose the fictional daily or monthly period.')
        day=self.now().date().isoformat();prefix=day if period=='daily' else day[:7]
        with self.db.read() as c:
            self._require(c,actor,'sales.view')
            rows=[dict(r) for r in c.execute('''SELECT kind,payment_method,COUNT(*) count,COALESCE(SUM(amount),0) total
                FROM unified_bookings WHERE substr(created_day,1,?)=? GROUP BY kind,payment_method ORDER BY kind''',(len(prefix),prefix))]
            return {'period':period,'date':prefix,'breakdown':rows,'total':sum(x['total'] for x in rows),'count':sum(x['count'] for x in rows),'mode':'simulated-only'}

    def notices(self,actor: int) -> dict:
        with self.db.read() as c:
            self._user_player(c,actor)
            waitlist=[dict(r) for r in c.execute('SELECT session_id,kind,message,created_at FROM unified_notifications WHERE user_id=? ORDER BY id DESC LIMIT 50',(actor,))]
            match=[dict(r) for r in c.execute('SELECT match_id,kind,subject,body,created_at FROM outbox WHERE user_id=? ORDER BY id DESC LIMIT 50',(actor,))]
            jobs=[dict(r) for r in c.execute('''SELECT j.kind,j.subject,j.body,j.state,j.attempts,j.created_at,o.match_id
                FROM unified_test_mail_jobs j LEFT JOIN outbox o
                  ON j.event_key='match:'||o.match_id||':'||o.user_id||':'||o.kind
                WHERE j.user_id=? ORDER BY j.id DESC LIMIT 100''',(actor,))]
            extra_jobs=[dict(r) for r in c.execute('''SELECT kind,subject,body,state,attempts,created_at,match_id
                FROM unified_test_extra_mail_jobs WHERE user_id=? ORDER BY id DESC LIMIT 100''',(actor,))]
            return {'waitlist':waitlist,'matches':match,'test_mail':jobs,'extra_test_mail':extra_jobs,
                    'message':'Fictional job records only; no email was sent.'}

    def create_rental_match(self,actor: int,booking_id: int,players: list[int]) -> int:
        if len(players)!=4 or len(set(players))!=4 or actor not in players:
            raise ValueError('Choose four distinct booked fictional players including yourself.')
        with self.db.tx() as c:
            self._user_player(c,actor)
            b=c.execute("SELECT * FROM unified_bookings WHERE id=? AND kind='court'",(booking_id,)).fetchone()
            if not b:raise ValueError('Fictional court booking not found.')
            for p in players:
                if Engine._user(c,p)['role']!='player' or not c.execute('SELECT 1 FROM unified_booking_players WHERE booking_id=? AND user_id=?',(booking_id,p)).fetchone():
                    raise PermissionDenied('Every rental match player must belong to this fictional booking.')
                if c.execute("""SELECT 1 FROM match_players mp JOIN matches m ON m.id=mp.match_id
                    WHERE mp.user_id=? AND m.status='active'""",(p,)).fetchone():raise Conflict('A fictional player is already in an active match.')
            if c.execute("SELECT 1 FROM matches WHERE court_id=? AND status='active'",(b['court_id'],)).fetchone():raise Conflict('That fictional court has an active match.')
            cur=c.execute("INSERT INTO matches(service,court_id,status,created_at) VALUES('court_rental',?,'active',?)",(b['court_id'],timestamp()))
            mid=int(cur.lastrowid)
            c.executemany('INSERT INTO match_players(match_id,user_id,team) VALUES(?,?,?)',[(mid,p,'A' if i<2 else 'B') for i,p in enumerate(players)])
            c.execute('INSERT INTO unified_rental_matches(match_id,booking_id) VALUES(?,?)',(mid,booking_id))
            self._audit(c,actor,'unified.rental_match',mid)
            return mid

    def queue(self,actor: int,session_id: int) -> list[dict]:
        with self.db.read() as c:
            user=Engine._user(c,actor)
            if not c.execute('SELECT 1 FROM unified_sessions WHERE session_id=?',(session_id,)).fetchone():raise ValueError('Fictional session not found.')
            if user['role']=='player':
                if not c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,actor)).fetchone():
                    raise PermissionDenied('A simulated registration is required.')
            else:self._require(c,actor,'queue.view')
            return [dict(r) for r in c.execute('''SELECT q.id,q.user_id,q.classification,q.state,q.source_match_id,u.display_name
                FROM queue_entries q JOIN users u ON u.id=q.user_id
                LEFT JOIN queue_positions pos ON pos.entry_id=q.id
                WHERE q.session_id=? AND q.state='waiting' ORDER BY COALESCE(pos.position,q.id),q.id''',(session_id,))]

    @staticmethod
    def queue_revision(session_id: int,rows) -> str:
        # Not a secret or authorization token; identifies the displayed order
        # *and* categories, so a newly confirmed result also invalidates it.
        text=f'{session_id}|' + '|'.join(f"{r['id']}:{r['classification']}" for r in rows)
        return hashlib.sha256(text.encode('ascii')).hexdigest()

    def reorder_queue(self,actor: int,session_id: int,expected_ids: list[int],ordered_ids: list[int],expected_revision: str) -> None:
        """Reorder only current waiting entries, with an optimistic stale-view check.

        In one SQLite write transaction, authorize the action, compare the exact
        current order and categories and write the full permutation. Concurrent
        check-ins, assignments, match ends, result classification changes and
        staff reorders invalidate the view. New check-ins still append. This is
        fictional single-database behavior, not a distributed queue.
        """
        if (not isinstance(expected_ids,list) or not isinstance(ordered_ids,list)
            or len(expected_ids)>100 or len(ordered_ids)>100
            or any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in [*expected_ids,*ordered_ids])
            or len(expected_ids)!=len(set(expected_ids)) or len(ordered_ids)!=len(set(ordered_ids))
            or set(expected_ids)!=set(ordered_ids)):
            raise ValueError('Provide one complete, distinct ordering of the fictional waiting entries.')
        with self.db.tx() as c:
            self._require(c,actor,'queue.manage')
            if not c.execute('SELECT 1 FROM unified_sessions WHERE session_id=?',(session_id,)).fetchone():
                raise ValueError('Fictional session not found.')
            rows=list(c.execute('''SELECT q.id,q.classification FROM queue_entries q
                LEFT JOIN queue_positions pos ON pos.entry_id=q.id
                WHERE q.session_id=? AND q.state='waiting' ORDER BY COALESCE(pos.position,q.id),q.id''',(session_id,)))
            current=[r['id'] for r in rows]
            if current!=expected_ids or self.queue_revision(session_id,rows)!=expected_revision:
                raise Conflict('The fictional queue changed. Refresh it before adjusting the order.')
            if ordered_ids==current:return
            c.executemany('''INSERT INTO queue_positions(entry_id,position) VALUES(?,?)
                ON CONFLICT(entry_id) DO UPDATE SET position=excluded.position''',
                [(entry_id,i) for i,entry_id in enumerate(ordered_ids,1)])
            self._audit(c,actor,'unified.queue_reordered',session_id)

    def matches(self,actor: int) -> list[dict]:
        with self.db.read() as c:
            user=Engine._user(c,actor);role=user['role']
            if role in ('player','superuser'):
                ownership=('WHERE EXISTS(SELECT 1 FROM match_players p WHERE p.match_id=m.id AND p.user_id=?)' if role=='player' else '')
                args=(actor,) if role=='player' else ()
                rows=c.execute(f'''SELECT m.*,r.state recording_state,r.file_path AS test_file_path,
                    t.expires_at AS sample_expires_at,t.state AS sample_retention_state
                    FROM matches m LEFT JOIN recordings r ON r.match_id=m.id
                    LEFT JOIN test_recording_retention t ON t.recording_id=r.id
                    {ownership} ORDER BY m.id DESC LIMIT 100''',args).fetchall()
            else:
                self._require(c,actor,'queue.view')
                rows=c.execute("SELECT m.*,NULL recording_state,NULL test_file_path,NULL sample_expires_at,NULL sample_retention_state FROM matches m WHERE m.service='openplay' ORDER BY m.id DESC LIMIT 100").fetchall()
            result=[]
            for row in rows:
                m=dict(row);m['players']=[dict(p) for p in c.execute('''SELECT p.user_id,p.team,u.display_name FROM match_players p
                    JOIN users u ON u.id=p.user_id WHERE p.match_id=? ORDER BY p.team,p.user_id''',(m['id'],))]
                participant=role=='player' and any(p['user_id']==actor for p in m['players'])
                m['can_control']=bool(participant or role=='superuser')
                file_available=bool(m.pop('test_file_path',None))
                retention_state=m.pop('sample_retention_state',None)
                expiry=m.get('sample_expires_at');unavailable=False
                if m['recording_state']=='ready' and expiry:
                    try:
                        when=datetime.fromisoformat(expiry)
                        unavailable=(when.tzinfo is None or retention_state!='scheduled' or datetime.now(timezone.utc)>=when)
                    except (TypeError,ValueError):unavailable=True
                if m['recording_state']=='ready' and (unavailable or not file_available):
                    m['recording_state']='expired' if expiry else 'unavailable'
                m['can_watch']=bool(m['can_control'] and m['recording_state']=='ready' and file_available)
                own_team=next((p['team'] for p in m['players'] if p['user_id']==actor),None) if participant else None
                team_confirmed=m.get('confirm_a_user') if own_team=='A' else m.get('confirm_b_user') if own_team=='B' else None
                m['can_confirm']=bool(participant and m['service']=='openplay' and m['score_state']=='pending' and team_confirmed is None)
                m['can_score']=bool(m['service']=='openplay' and m['status']=='ended' and m['score_state']!='confirmed' and (participant or Engine._is_authorized(c,actor,'score.enter')))
                m['can_end']=bool(m['status']=='active' and (participant or Engine._is_authorized(c,actor,'queue.manage')))
                result.append(m)
            return result
