"""Persistent player alerts and extra email jobs for FICTIONAL tests only.

An in-site alert is independent from email delivery. Queue assignments, ready
stock-sample replays and confirmed Open Play rankings are captured atomically
with their test events. Queue and rental-replay email jobs use a separate,
additive .invalid-only outbox, so old fictional mail tables need no destructive
migration. Nothing in this module sends email, connects a camera or processes
real customers. No historical alerts are backfilled when this schema is added.
"""
from __future__ import annotations

from lg_sports_deck.core.domain import PermissionDenied
from .domain import UnifiedDemo


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS unified_test_player_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK(kind IN ('queue_assignment','video_ready','ranking_update')),
  match_id INTEGER NOT NULL REFERENCES matches(id),
  session_id INTEGER REFERENCES openplay_sessions(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  read_at INTEGER
);
CREATE INDEX IF NOT EXISTS unified_test_player_alerts_user
  ON unified_test_player_alerts(user_id,read_at,id);

-- Separate additive outbox: no alteration of existing fictional mail/receipts.
CREATE TABLE IF NOT EXISTS unified_test_extra_mail_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('queue_assignment','rental_replay')),
  match_id INTEGER NOT NULL REFERENCES matches(id),
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
CREATE INDEX IF NOT EXISTS unified_test_extra_mail_due
  ON unified_test_extra_mail_jobs(state,available_at,id);
CREATE TABLE IF NOT EXISTS unified_test_extra_mail_receipts (
  job_id INTEGER PRIMARY KEY REFERENCES unified_test_extra_mail_jobs(id),
  simulated_at INTEGER NOT NULL
);

-- This test queue alert is an assignment, not a prediction of an unconfirmed
-- future position. Only unified Open Play matches create this fictional event.
CREATE TRIGGER IF NOT EXISTS unified_test_player_assignment_alert
AFTER INSERT ON match_players
WHEN EXISTS (SELECT 1 FROM matches m JOIN unified_sessions s ON s.session_id=m.session_id
             WHERE m.id=NEW.match_id AND m.service='openplay')
BEGIN
  INSERT OR IGNORE INTO unified_test_player_alerts
    (event_key,user_id,kind,match_id,session_id,title,body,created_at)
  SELECT 'assignment:'||m.id||':'||NEW.user_id,NEW.user_id,'queue_assignment',m.id,m.session_id,
    '[TEST ONLY] Your Open Play match is assigned',
    'Test match '||m.id||' is assigned to Court '||printf('%02d',m.court_id)||
    ', Team '||NEW.team||'. Open your match for details. This is a fictional queue.',
    COALESCE(CAST(strftime('%s',m.created_at) AS INTEGER),0)
  FROM matches m WHERE m.id=NEW.match_id;
  INSERT OR IGNORE INTO unified_test_extra_mail_jobs
    (event_key,user_id,recipient,kind,match_id,subject,body,created_at,updated_at)
  SELECT 'assignment:'||m.id||':'||u.id,u.id,u.email,'queue_assignment',m.id,
    '[TEST ONLY] Your Open Play match is assigned',
    'TEST ONLY: Fictional match '||m.id||' is assigned to Court '||printf('%02d',m.court_id)||
    ', Team '||NEW.team||'. Sign in to the test portal. No actual email was sent.',
    COALESCE(CAST(strftime('%s',m.created_at) AS INTEGER),0),
    COALESCE(CAST(strftime('%s',m.created_at) AS INTEGER),0)
  FROM matches m JOIN users u ON u.id=NEW.user_id WHERE m.id=NEW.match_id;
END;

-- The core outbox is inserted only when the sample clip is finalized or both
-- Open Play teams confirm the score. Existing result/replay *email* jobs remain
-- the sole mail job for those events; this trigger adds only in-site alerts.
CREATE TRIGGER IF NOT EXISTS unified_test_player_openplay_alert
AFTER INSERT ON outbox
WHEN NEW.kind IN ('replay','result') AND EXISTS (
  SELECT 1 FROM matches m JOIN unified_sessions s ON s.session_id=m.session_id
  WHERE m.id=NEW.match_id AND m.service='openplay'
)
BEGIN
  INSERT OR IGNORE INTO unified_test_player_alerts
    (event_key,user_id,kind,match_id,session_id,title,body,created_at)
  SELECT 'openplay:'||NEW.match_id||':'||NEW.user_id||':'||NEW.kind,NEW.user_id,
    CASE NEW.kind WHEN 'replay' THEN 'video_ready' ELSE 'ranking_update' END,
    m.id,m.session_id,
    CASE NEW.kind WHEN 'replay' THEN '[TEST ONLY] Your sample replay is ready'
                  ELSE '[TEST ONLY] Your Open Play result is confirmed' END,
    CASE NEW.kind WHEN 'replay' THEN
      'The labelled stock sample for test match '||m.id||' is ready, even if the score is pending. It is not your match footage.'
    ELSE
      'Both teams confirmed test match '||m.id||' ('||m.score_a||'–'||m.score_b||
      '). Your fictional wins and win rate are updated in Rankings.' END,
    COALESCE(CAST(strftime('%s',NEW.created_at) AS INTEGER),0)
  FROM matches m WHERE m.id=NEW.match_id;
END;

-- For the internal sample only, notify booked rental participants when their
-- stock replay is finalized. Final live rental email-recipient policy remains
-- subject to confirmation; this fixture does not decide or send it.
CREATE TRIGGER IF NOT EXISTS unified_test_player_rental_replay_alert
AFTER UPDATE OF state ON recordings
WHEN NEW.state='ready' AND OLD.state<>'ready' AND EXISTS (
  SELECT 1 FROM matches m JOIN unified_rental_matches r ON r.match_id=m.id
  WHERE m.id=NEW.match_id AND m.service='court_rental'
)
BEGIN
  INSERT OR IGNORE INTO unified_test_player_alerts
    (event_key,user_id,kind,match_id,session_id,title,body,created_at)
  SELECT 'rental_replay:'||NEW.match_id||':'||u.id,u.id,'video_ready',NEW.match_id,NULL,
    '[TEST ONLY] Your rental sample replay is ready',
    'The labelled stock sample for fictional rental match '||NEW.match_id||
    ' is ready. It is not your match footage.',
    COALESCE(CAST(strftime('%s',NEW.ready_at) AS INTEGER),0)
  FROM match_players p JOIN users u ON u.id=p.user_id WHERE p.match_id=NEW.match_id;
  INSERT OR IGNORE INTO unified_test_extra_mail_jobs
    (event_key,user_id,recipient,kind,match_id,subject,body,created_at,updated_at)
  SELECT 'rental_replay:'||NEW.match_id||':'||u.id,u.id,u.email,'rental_replay',NEW.match_id,
    '[TEST ONLY] Your rental sample replay is ready',
    'TEST ONLY: The labelled stock sample for fictional rental match '||NEW.match_id||
    ' is ready. It is not your match footage. No actual email was sent.',
    COALESCE(CAST(strftime('%s',NEW.ready_at) AS INTEGER),0),
    COALESCE(CAST(strftime('%s',NEW.ready_at) AS INTEGER),0)
  FROM match_players p JOIN users u ON u.id=p.user_id WHERE p.match_id=NEW.match_id;
END;
"""


class FictionalPlayerAlerts:
    """Player-owned, server-authorized in-site alerts; no external delivery."""
    def __init__(self,service: UnifiedDemo):
        self.service=service;self.db=service.db
        with self.db.read() as c:c.executescript(SCHEMA)

    def list(self,actor: int) -> dict:
        with self.db.read() as c:
            self.service._user_player(c,actor)
            unread=c.execute('SELECT COUNT(*) FROM unified_test_player_alerts WHERE user_id=? AND read_at IS NULL',(actor,)).fetchone()[0]
            # Prioritize unread items, so old unread alerts do not become
            # permanently unreachable when a player has over 100 updates.
            rows=c.execute('''SELECT id,kind,match_id,session_id,title,body,created_at,read_at
                FROM unified_test_player_alerts WHERE user_id=?
                ORDER BY (read_at IS NOT NULL),id DESC LIMIT 100''',(actor,)).fetchall()
            return {'alerts':[dict(r) for r in rows],'unread_count':int(unread),'mode':'fictional-only'}

    def mark_read(self,actor: int,alert_id: int) -> None:
        if type(alert_id) is not int or alert_id<1:raise ValueError('Invalid fictional alert.')
        with self.db.tx() as c:
            self.service._user_player(c,actor)
            row=c.execute('SELECT read_at FROM unified_test_player_alerts WHERE id=? AND user_id=?',(alert_id,actor)).fetchone()
            if not row:raise PermissionDenied('This fictional notification is unavailable.')
            if row['read_at'] is None:
                c.execute('UPDATE unified_test_player_alerts SET read_at=? WHERE id=? AND user_id=?',
                          (int(self.service.clock()),alert_id,actor))
