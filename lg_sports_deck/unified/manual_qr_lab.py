"""Fictional manual QR laboratory, OFF and unmounted unless explicitly enabled.

This is NOT an actual QR payment integration or a live payment verifier. It
accepts only invented .invalid accounts and a fake `FAKE-QR-...` reference.
No user-supplied QR, real transaction, bank account, credential, screenshot,
network call or email delivery is used. A fictional Superuser or individually
authorized test staff member must explicitly assert a fictional account-side
check; fake images are never accepted as proof.

The quoted slot is NOT held. A fake received payment whose slot, price, account
or amount is no longer valid is preserved for review instead of being lost or
creating a booking. Verification order does NOT prove first actual payment.
This test cannot be used for a real payment, refund, settlement or production.
"""
from __future__ import annotations

from datetime import date
import hashlib
import hmac
import json
import re
import sqlite3

from lg_sports_deck.accounts.service import normalize_test_email
from lg_sports_deck.core.domain import Conflict, Engine, PermissionDenied
from .domain import UnifiedDemo

KEY_RE=re.compile(r'^[A-Za-z0-9_-]{16,100}$',re.ASCII)
REFERENCE_RE=re.compile(r'^FAKE-QR-[A-Za-z0-9_-]{8,80}$',re.ASCII)


class FictionalManualQR:
    """Fictional intent/review state machine; not a real financial service."""
    __test__=False
    def __init__(self,svc: UnifiedDemo, *, require_player_claim: bool=False):
        if type(require_player_claim) is not bool:raise ValueError('Invalid fictional lab configuration.')
        self.svc=svc;self.db=svc.db;self.require_player_claim=require_player_claim
        with self.db.read() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS unified_test_manual_qr_intents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_id INTEGER NOT NULL REFERENCES users(id),
          idempotency_key TEXT NOT NULL,
          request_json TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          amount INTEGER NOT NULL CHECK(amount>0),
          currency TEXT NOT NULL CHECK(currency='PHP'),
          state TEXT NOT NULL CHECK(state IN ('pending','confirmed','manual_review','rejected')),
          reference_hash TEXT UNIQUE,
          observed_amount INTEGER,
          booking_id INTEGER REFERENCES unified_bookings(id),
          reviewer_id INTEGER REFERENCES users(id),
          issue TEXT,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(actor_id,idempotency_key),
          CHECK((state='confirmed' AND booking_id IS NOT NULL AND reference_hash IS NOT NULL)
             OR (state<>'confirmed' AND booking_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS unified_test_manual_qr_claims (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          intent_id INTEGER NOT NULL REFERENCES unified_test_manual_qr_intents(id),
          reference_hash TEXT NOT NULL,
          reference_suffix TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS unified_test_manual_claims_latest
          ON unified_test_manual_qr_claims(intent_id,id DESC);
        ''')

    @staticmethod
    def _fictional_user(c,uid: int):
        u=Engine._user(c,uid)
        if normalize_test_email(u['email'])!=u['email']:
            raise ValueError('Only fictional .invalid accounts are allowed in this laboratory.')
        return u

    def _reviewer(self,c,actor: int):
        self._fictional_user(c,actor)
        # The Superuser can delegate this individual fictional action to Admin
        # or Manager. This fictional lab does not authorize real payment review.
        if not Engine._is_authorized(c,actor,'payments.manual_verify'):
            raise PermissionDenied('Fictional manual-payment review permission is required.')

    @staticmethod
    def _integer(value,minimum=1):
        if type(value) is not int or value<minimum:raise ValueError('Invalid fictional payment request.')
        return value

    def _normalize_request(self,kind: str,payload: dict) -> dict:
        if not isinstance(payload,dict) or kind not in ('openplay','court'):
            raise ValueError('Invalid fictional payment request.')
        expected={'session_id','player_ids'} if kind=='openplay' else {'day','start_hour','duration','court','player_ids'}
        if set(payload)!=expected:raise ValueError('Unexpected fictional request fields.')
        players=payload['player_ids']
        if not isinstance(players,list) or not 1<=len(players)<=100 or any(type(p) is not int or p<1 for p in players):
            raise ValueError('Use fictional player IDs only.')
        if len(players)!=len(set(players)):raise ValueError('Fictional players must be distinct.')
        if kind=='openplay':return {'kind':kind,'session_id':self._integer(payload['session_id']),'player_ids':players}
        try:
            if type(payload['day']) is not str:raise ValueError()
            day=date.fromisoformat(payload['day'])
        except ValueError as exc:raise ValueError('Use a fictional ISO booking day.') from exc
        return {'kind':kind,'day':day.isoformat(),'start_hour':self._integer(payload['start_hour']),
                'duration':self._integer(payload['duration']),'court':self._integer(payload['court']),'player_ids':players}

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {'intent_id':row['id'],'state':row['state'],'amount':row['amount'],'currency':row['currency'],
                'booking_id':row['booking_id'],'issue':row['issue'],'mode':'fictional-manual-qr-only'}

    def initiate(self,actor: int,idempotency_key: str,kind: str,payload: dict) -> dict:
        if not isinstance(idempotency_key,str) or not KEY_RE.fullmatch(idempotency_key):
            raise ValueError('Use a fictional idempotency key.')
        req=self._normalize_request(kind,payload)
        serialized=json.dumps(req,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        digest=hashlib.sha256(serialized.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            u=self._fictional_user(c,actor)
            if u['role']!='player':raise PermissionDenied('A fictional player must request this test checkout.')
            for pid in req['player_ids']:self._fictional_user(c,pid)
            existing=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE actor_id=? AND idempotency_key=?',(actor,idempotency_key)).fetchone()
            if existing:
                if not hmac.compare_digest(existing['request_hash'],digest):
                    raise Conflict('This fictional key already identifies a different request.')
                return self._public(existing)
            if kind=='openplay':_,amount=self.svc._quote_openplay_in_tx(c,actor,req['session_id'],req['player_ids'])
            else:_,_,_,amount=self.svc._quote_court_in_tx(c,actor,date.fromisoformat(req['day']),req['start_hour'],req['duration'],req['court'],req['player_ids'])
            now=int(self.svc.clock())
            cur=c.execute('''INSERT INTO unified_test_manual_qr_intents
              (actor_id,idempotency_key,request_json,request_hash,amount,currency,state,created_at,updated_at)
              VALUES(?,?,?,?,?,'PHP','pending',?,?)''',(actor,idempotency_key,serialized,digest,amount,now,now))
            return self._public(c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(cur.lastrowid,)).fetchone())

    def read(self,actor: int,intent_id: int) -> dict:
        self._integer(intent_id)
        with self.db.read() as c:
            u=self._fictional_user(c,actor)
            row=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone()
            staff=bool(u['role'] in ('admin','manager') and Engine._is_authorized(c,actor,'payments.manual_verify'))
            if not row or not (u['role']=='superuser' or staff or (u['role']=='player' and row['actor_id']==actor)):
                raise PermissionDenied('This fictional payment request is unavailable.')
            return self._public(row)

    @staticmethod
    def _latest_claim(c,intent_id: int):
        return c.execute('''SELECT reference_hash,reference_suffix FROM unified_test_manual_qr_claims
                            WHERE intent_id=? ORDER BY id DESC LIMIT 1''',(intent_id,)).fetchone()

    def _display(self,c,row: sqlite3.Row) -> dict:
        """Small authorized display, without storing or returning a full fake reference."""
        req=json.loads(row['request_json']);claim=self._latest_claim(c,row['id'])
        booker=c.execute('SELECT display_name FROM users WHERE id=?',(row['actor_id'],)).fetchone()
        players=[]
        for pid in req['player_ids']:
            person=c.execute('SELECT display_name FROM users WHERE id=?',(pid,)).fetchone()
            players.append(person['display_name'] if person else 'Unavailable fictional player')
        if req['kind']=='openplay':
            session=c.execute('''SELECT s.label,t.day,t.start_min,t.end_min FROM openplay_sessions s
                JOIN unified_sessions t ON t.session_id=s.id WHERE s.id=?''',(req['session_id'],)).fetchone()
            detail={'kind':'openplay',**(dict(session) if session else {})}
        else:
            detail={'kind':'court','day':req['day'],'start_min':req['start_hour']*60,
                    'end_min':(req['start_hour']+req['duration'])*60,'court':req['court']}
        return {**self._public(row),'test_booker_id':row['actor_id'],
                'test_booker_name':booker['display_name'] if booker else 'Unavailable fictional player',
                'test_players':players,'test_request':req,'test_schedule':detail,
                'claim_submitted':bool(claim),'claim_suffix':claim['reference_suffix'] if claim else None}

    def list_mine(self,actor: int) -> dict:
        with self.db.read() as c:
            if self._fictional_user(c,actor)['role']!='player':raise PermissionDenied('A fictional player account is required.')
            rows=list(c.execute('''SELECT * FROM unified_test_manual_qr_intents WHERE actor_id=?
                ORDER BY CASE state WHEN 'pending' THEN 0 WHEN 'manual_review' THEN 1 ELSE 2 END,id DESC LIMIT 101''',(actor,)))
            return {'requests':[self._display(c,row) for row in rows[:100]],'has_more':len(rows)>100,'mode':'fictional-only'}

    def list_for_review(self,reviewer: int) -> dict:
        with self.db.read() as c:
            self._reviewer(c,reviewer)
            rows=list(c.execute('''SELECT i.* FROM unified_test_manual_qr_intents i
                WHERE i.state IN ('pending','manual_review')
                ORDER BY CASE WHEN i.state='manual_review' OR EXISTS
                  (SELECT 1 FROM unified_test_manual_qr_claims q WHERE q.intent_id=i.id) THEN 0 ELSE 1 END,
                  i.created_at,i.id LIMIT 101'''))
            return {'requests':[self._display(c,row) for row in rows[:100]],'has_more':len(rows)>100,'mode':'fictional-only'}

    def submit_claim(self,actor: int,intent_id: int,reference: str) -> dict:
        """A player's fake claim is NOT evidence that a transaction succeeded."""
        self._integer(intent_id)
        if not isinstance(reference,str) or not REFERENCE_RE.fullmatch(reference):
            raise ValueError('Use a fictional FAKE-QR- reference only; never enter a real transaction.')
        digest=hashlib.sha256(reference.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            u=self._fictional_user(c,actor)
            row=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone()
            if not row or u['role']!='player' or row['actor_id']!=actor:
                raise PermissionDenied('This fictional payment request is unavailable.')
            if row['state']!='pending':raise Conflict('This fictional request is no longer awaiting a claim.')
            previous=self._latest_claim(c,intent_id)
            if not previous or not hmac.compare_digest(previous['reference_hash'],digest):
                c.execute('''INSERT INTO unified_test_manual_qr_claims(intent_id,reference_hash,reference_suffix,created_at)
                    VALUES(?,?,?,?)''',(intent_id,digest,reference[-4:],int(self.svc.clock())))
                self.svc._audit(c,actor,'unified.fictional_manual_qr_claimed',intent_id)
            return self._display(c,row)

    def review_details(self,reviewer: int,intent_id: int) -> dict:
        """Return fictional checkout details only to an authorized reviewer."""
        self._integer(intent_id)
        with self.db.read() as c:
            self._reviewer(c,reviewer)
            row=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone()
            if not row:raise ValueError('Fictional payment request not found.')
            return self._display(c,row)

    def confirm_account_check(self,reviewer: int,intent_id: int,reference: str,observed_amount: int,*,account_checked: bool=False) -> dict:
        """Simulate manual confirmation; never rely on a payment screenshot."""
        self._integer(intent_id);self._integer(observed_amount)
        if account_checked is not True or not isinstance(reference,str) or not REFERENCE_RE.fullmatch(reference):
            raise ValueError('Explicit fictional account verification and a FAKE-QR reference are required.')
        ref_hash=hashlib.sha256(reference.encode('ascii')).hexdigest()
        with self.db.tx() as c:
            self._reviewer(c,reviewer)
            row=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone()
            if not row:raise ValueError('Fictional payment request not found.')
            if row['state']!='pending':
                if (row['reference_hash'] and hmac.compare_digest(row['reference_hash'],ref_hash)
                    and row['observed_amount']==observed_amount and row['state'] in ('confirmed','manual_review')):
                    return self._public(row)
                raise Conflict('This fictional request was already reviewed.')
            if self.require_player_claim:
                claim=self._latest_claim(c,intent_id)
                if not claim:raise Conflict('The fictional player has not submitted a test reference yet.')
                if not hmac.compare_digest(claim['reference_hash'],ref_hash):
                    raise Conflict('The independently checked fake reference does not match the player claim. Do not confirm.')
            duplicate=c.execute('SELECT id FROM unified_test_manual_qr_intents WHERE reference_hash=?',(ref_hash,)).fetchone()
            if duplicate:raise Conflict('That fictional transaction reference was already used; investigate before proceeding.')
            now=int(self.svc.clock());issue=None;booking=None
            if observed_amount!=row['amount']:
                issue='amount_mismatch'
            else:
                req=json.loads(row['request_json'])
                # Roll back any partially inserted test booking while retaining
                # the observed fake payment as a review exception.
                c.execute('SAVEPOINT fictional_booking')
                try:
                    if req['kind']=='openplay':
                        booking=self.svc._book_openplay_in_tx(c,row['actor_id'],req['session_id'],req['player_ids'],expected_amount=row['amount'])
                    else:
                        booking=self.svc._book_court_in_tx(c,row['actor_id'],date.fromisoformat(req['day']),
                            req['start_hour'],req['duration'],req['court'],req['player_ids'],expected_amount=row['amount'])
                except (Conflict,PermissionDenied,ValueError):
                    c.execute('ROLLBACK TO fictional_booking');c.execute('RELEASE fictional_booking')
                    issue='paid_but_unbookable_or_changed'
                else:c.execute('RELEASE fictional_booking')
            state='manual_review' if issue else 'confirmed'
            c.execute('''UPDATE unified_test_manual_qr_intents SET state=?,reference_hash=?,observed_amount=?,
                         booking_id=?,reviewer_id=?,issue=?,updated_at=? WHERE id=? AND state='pending' ''',
                      (state,ref_hash,observed_amount,booking['booking_id'] if booking else None,reviewer,issue,now,intent_id))
            self.svc._audit(c,reviewer,'unified.fictional_manual_qr_reviewed',intent_id)
            return self._public(c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone())

    def reject(self,reviewer: int,intent_id: int) -> dict:
        self._integer(intent_id)
        with self.db.tx() as c:
            self._reviewer(c,reviewer)
            row=c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone()
            if not row:raise ValueError('Fictional payment request not found.')
            if row['state']=='rejected':return self._public(row)
            if row['state']!='pending':raise Conflict('This fictional request was already reviewed.')
            c.execute("UPDATE unified_test_manual_qr_intents SET state='rejected',reviewer_id=?,updated_at=? WHERE id=?",
                      (reviewer,int(self.svc.clock()),intent_id))
            self.svc._audit(c,reviewer,'unified.fictional_manual_qr_rejected',intent_id)
            return self._public(c.execute('SELECT * FROM unified_test_manual_qr_intents WHERE id=?',(intent_id,)).fetchone())
