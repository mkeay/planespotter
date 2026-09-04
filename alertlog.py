#!/usr/bin/env python3
"""SQLite alert history for planespotter.

Every alert that reaches IRC is appended here along with the full aircraft
record it was built from. The raw JSON column is deliberate: it means a
question asked of the corpus later isn't limited to the fields that seemed
worth a column today.

Receiver up/down transitions go in a second table. Without them an analysis
can't separate "nothing interesting flew over" from "the dongle was
unplugged" -- which is exactly the distinction that matters when scoring how
often an online alert is followed by a local pass. Stdlib only."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("spotter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    hex         TEXT,
    flight      TEXT,
    reg         TEXT,
    type        TEXT,
    kind        TEXT,
    tag         TEXT,
    source      TEXT,
    alt_ft      INTEGER,
    gs_kt       REAL,
    squawk      TEXT,
    lat         REAL,
    lon         REAL,
    dist_nm     REAL,
    bearing_deg REAL,
    military    INTEGER,
    mlat        INTEGER,
    emergency   INTEGER,
    category    TEXT,
    message     TEXT,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts  ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_hex ON alerts(hex);

CREATE TABLE IF NOT EXISTS receiver_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    state  TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_receiver_ts ON receiver_events(ts);
"""

COLUMNS = ("ts", "hex", "flight", "reg", "type", "kind", "tag", "source",
           "alt_ft", "gs_kt", "squawk", "lat", "lon", "dist_nm", "bearing_deg",
           "military", "mlat", "emergency", "category", "message", "raw")

INSERT_ALERT = (f"INSERT INTO alerts ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(COLUMNS))})")


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlertLog:
    """Append-only history of what was alerted and when.

    Disabled -- every method a no-op -- when no path is configured, so the
    bot runs exactly as before without one.

    Nothing here may take the bot down. An alert that reached IRC matters
    more than its database row, so writes are best-effort: a failure is
    logged once and the log then goes quiet rather than raising into the
    alert path or filling the journal with its own complaints."""

    def __init__(self, path):
        self.path = str(path) if path else ""
        self.conn = None
        self.complained = False
        if not self.path:
            return
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=5,
                                        check_same_thread=False)
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            log.info("Alert log: %s", self.path)
        except (sqlite3.Error, OSError) as e:
            log.warning("Could not open alert log %s: %s", self.path, e)
            self.conn = None

    def _write(self, sql, values):
        if self.conn is None:
            return
        try:
            self.conn.execute(sql, values)
            self.conn.commit()
        except sqlite3.Error as e:
            if not self.complained:
                log.warning("Alert log write failed (further errors silent): %s", e)
                self.complained = True

    def record_alert(self, ac, message, kind="Alert", tag=None, source=None,
                     dist_nm=None, bearing_deg=None, altitude=None,
                     military=False, mlat=False, emergency=False):
        """One row per line sent to the channel."""
        if self.conn is None:
            return
        try:
            raw = json.dumps(ac, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            raw = None
        self._write(INSERT_ALERT, (
            _utcnow(),
            (ac.get("hex") or "").lower() or None,
            (ac.get("flight") or "").strip() or None,
            ac.get("r"),
            ac.get("t"),
            kind,
            tag,
            source,
            altitude,
            ac.get("gs"),
            ac.get("squawk"),
            ac.get("lat"),
            ac.get("lon"),
            dist_nm,
            bearing_deg,
            int(bool(military)),
            int(bool(mlat)),
            int(bool(emergency)),
            ac.get("category"),
            message,
            raw,
        ))

    def record_receiver(self, state, detail=None):
        """One row per up/down transition of the local feed."""
        self._write(
            "INSERT INTO receiver_events (ts, state, detail) VALUES (?, ?, ?)",
            (_utcnow(), state, detail))
