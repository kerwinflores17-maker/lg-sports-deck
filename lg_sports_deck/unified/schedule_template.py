"""Fictional daily schedule template — single repeating day + per-date override.

This is NOT a production scheduler. It stores a repeating day of
6 AM–midnight slices as either Open Play (both courts blocked) or
Court Rental windows. Superuser edits the repeating template;
optionally overrides a single date. Reports group fictional bookings
by the slot they fall in.

No real calendar, timezone event, or payment reconciliation is implied.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime
from typing import Callable

from lg_sports_deck.core.domain import Database, Engine, PermissionDenied
from lg_sports_deck.accounts.service import Accounts

DEFAULT_SLOTS = [
    {"start_hour": 6, "duration": 2, "kind": "rental", "label": "6-8 AM Rental"},
    {"start_hour": 8, "duration": 3, "kind": "open_play", "label": "8-11 AM Open Play"},
    {"start_hour": 11, "duration": 2, "kind": "rental", "label": "11 AM-1 PM Rental"},
    {"start_hour": 13, "duration": 3, "kind": "open_play", "label": "1-4 PM Open Play"},
    {"start_hour": 16, "duration": 3, "kind": "open_play", "label": "4-7 PM Open Play"},
    {"start_hour": 19, "duration": 3, "kind": "open_play", "label": "7-10 PM Open Play"},
    {"start_hour": 22, "duration": 2, "kind": "rental", "label": "10 PM-12 AM Rental"},
]

SLOT_KINDS = {"open_play", "rental"}

def _validate_slots(slots):
    if not isinstance(slots, list) or not slots:
        raise ValueError("Template needs at least one slot.")
    if len(slots) > 24:
        raise ValueError("Too many slots.")
    seen = []
    for s in slots:
        if not isinstance(s, dict):
            raise ValueError("Each slot must be an object.")
        sh = s.get("start_hour")
        du = s.get("duration")
        kind = s.get("kind")
        label = s.get("label", "")
        if not isinstance(sh, int) or isinstance(sh, bool) or not 6 <= sh <= 23:
            raise ValueError("start_hour must be 6-23.")
        if not isinstance(du, int) or isinstance(du, bool) or not 1 <= du <= 18:
            raise ValueError("duration must be 1-18 hours.")
        if kind not in SLOT_KINDS:
            raise ValueError("kind must be open_play or rental.")
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError("label must be 0-80 chars.")
        start = sh * 60
        end = start + du * 60
        if end > 1440 or start < 360:
            raise ValueError("Slot must be within 6 AM–midnight.")
        seen.append((start, end, kind, label.strip()))
    # Check coverage and no gaps/overlaps for the repeating day (must cover 6-24 exactly, no overlap, no gap)
    # For the fictional template we require exact tiling from 360 to 1440
    seen_sorted = sorted(seen, key=lambda x: x[0])
    cursor = 360
    for start, end, _, _ in seen_sorted:
        if start != cursor:
            raise ValueError(f"Slots must tile 6 AM–midnight without gaps or overlaps; gap at {cursor//60}:00.")
        cursor = end
    if cursor != 1440:
        raise ValueError("Slots must cover exactly 6 AM–midnight (18 hours).")
    return seen_sorted

class ScheduleTemplate:
    __test__ = False
    def __init__(self, db: Database, accounts: Accounts):
        self.db = db
        self.accounts = accounts
        with db.read() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS schedule_template (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              slots_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedule_overrides (
              day TEXT PRIMARY KEY,
              slots_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """)
            row = c.execute("SELECT slots_json FROM schedule_template WHERE singleton=1").fetchone()
            if not row:
                now = int(datetime.now().timestamp())
                c.execute("SELECT 1")  # ensure read tx, but we need write for insert
        # Insert default if missing (use tx)
        with db.tx() as c:
            if not c.execute("SELECT 1 FROM schedule_template WHERE singleton=1").fetchone():
                now = int(datetime.now().timestamp())
                c.execute("INSERT INTO schedule_template(singleton, slots_json, updated_at) VALUES(1,?,?)",
                          (json.dumps(DEFAULT_SLOTS), now))

    def _require_superuser(self, c, actor: int):
        u = Engine._user(c, actor)
        if u['role'] != 'superuser':
            raise PermissionDenied("Only Superuser can edit the daily schedule.")

    def get_template(self) -> list[dict]:
        with self.db.read() as c:
            row = c.execute("SELECT slots_json FROM schedule_template WHERE singleton=1").fetchone()
            return json.loads(row['slots_json']) if row else list(DEFAULT_SLOTS)

    def set_template(self, actor: int, slots: list[dict]):
        validated = _validate_slots(slots)
        # Normalize to dicts with start_hour/duration/kind/label
        normalized = []
        for start, end, kind, label in validated:
            normalized.append({
                "start_hour": start // 60,
                "duration": (end - start) // 60,
                "kind": kind,
                "label": label,
                "start_min": start,
                "end_min": end,
            })
        # Keep original shape for storage (start_hour/duration/kind/label)
        storage = [{"start_hour": s["start_hour"], "duration": s["duration"], "kind": s["kind"], "label": s["label"]} for s in normalized]
        with self.db.tx() as c:
            self._require_superuser(c, actor)
            c.execute("UPDATE schedule_template SET slots_json=?, updated_at=? WHERE singleton=1",
                      (json.dumps(storage), int(datetime.now().timestamp())))

    def get_day_schedule(self, day: date | str) -> list[dict]:
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        with self.db.read() as c:
            row = c.execute("SELECT slots_json FROM schedule_overrides WHERE day=?", (day_str,)).fetchone()
            if row:
                slots = json.loads(row['slots_json'])
            else:
                row2 = c.execute("SELECT slots_json FROM schedule_template WHERE singleton=1").fetchone()
                slots = json.loads(row2['slots_json']) if row2 else list(DEFAULT_SLOTS)
        # Enrich with start_min/end_min
        enriched = []
        for s in slots:
            sh = s['start_hour']; du = s['duration']
            enriched.append({**s, "start_min": sh*60, "end_min": (sh+du)*60})
        return enriched

    def set_day_override(self, actor: int, day: date | str, slots: list[dict]):
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        # validate date range like UnifiedDemo._day but allow any ISO date for template
        validated = _validate_slots(slots)
        storage = [{"start_hour": s[0]//60, "duration": (s[1]-s[0])//60, "kind": s[2], "label": s[3]} for s in validated]
        with self.db.tx() as c:
            self._require_superuser(c, actor)
            c.execute("INSERT OR REPLACE INTO schedule_overrides(day, slots_json, updated_at) VALUES(?,?,?)",
                      (day_str, json.dumps(storage), int(datetime.now().timestamp())))

    def clear_day_override(self, actor: int, day: date | str):
        day_str = day.isoformat() if isinstance(day, date) else str(day)
        with self.db.tx() as c:
            self._require_superuser(c, actor)
            c.execute("DELETE FROM schedule_overrides WHERE day=?", (day_str,))

    def sales_by_schedule(self, actor: int, period: str, today: date | None = None) -> list[dict]:
        """Group fictional sales by the slot they fall in.

        period: 'daily' -> today (or provided today), 'monthly' -> YYYY-MM prefix of today
        Returns list of {label, kind, start_hour, duration, bookings, revenue}
        """
        if period not in ('daily', 'monthly'):
            raise ValueError("period must be daily or monthly")
        # Permission: only superuser or sales.view
        with self.db.read() as c:
            u = Engine._user(c, actor)
            if u['role'] not in ('superuser',) and not Engine._is_authorized(c, actor, 'sales.view'):
                raise PermissionDenied("This fictional account lacks the required action permission.")
            # Determine prefix
            # Use UnifiedDemo.now date via DB? For simplicity use today param or now
            if today is None:
                # Try to infer from now via python date today
                today = datetime.now().date()
            if isinstance(today, str):
                today = date.fromisoformat(today)
            if period == 'daily':
                prefix = today.isoformat()
                like = prefix + "%"
            else:
                prefix = today.isoformat()[:7]  # YYYY-MM
                like = prefix + "%"

            # Fetch bookings in period (created_day prefix)
            bookings = list(c.execute(
                "SELECT day, start_min, end_min, kind, amount, created_day FROM unified_bookings WHERE created_day LIKE ?",
                (like,)
            ))
        # For each booking, find which slot its start_min falls in for its day
        # Use day-specific schedule (with override)
        from collections import defaultdict
        agg = defaultdict(lambda: {"label": "", "kind": "", "start_hour": 0, "duration": 0, "bookings": 0, "revenue": 0})
        for b in bookings:
            day_str = b['day']
            start_min = b['start_min']
            # get schedule for that booking's day
            slots = self.get_day_schedule(day_str)
            matched = None
            for s in slots:
                if s['start_min'] <= start_min < s['end_min']:
                    matched = s
                    break
            if not matched:
                # booking outside template (should not happen, but skip)
                continue
            key = (matched['label'], matched['kind'], matched['start_hour'], matched['duration'])
            entry = agg[key]
            entry['label'] = matched['label']
            entry['kind'] = matched['kind']
            entry['start_hour'] = matched['start_hour']
            entry['duration'] = matched['duration']
            entry['bookings'] += 1
            entry['revenue'] += int(b['amount'])

        # Also include zero-revenue slots for completeness (for the template's slots)
        # Use the template's slots as base (not per-day) for display
        base_slots = self.get_day_schedule(today.isoformat() if isinstance(today, date) else str(today))
        # Ensure each base slot appears even if zero
        for s in base_slots:
            key = (s['label'], s['kind'], s['start_hour'], s['duration'])
            if key not in agg:
                agg[key] = {"label": s['label'], "kind": s['kind'], "start_hour": s['start_hour'], "duration": s['duration'], "bookings": 0, "revenue": 0}

        # Sort by start_hour
        result = sorted(agg.values(), key=lambda x: x['start_hour'])
        # Add total
        return result
