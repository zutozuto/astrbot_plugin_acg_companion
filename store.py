"""Small, bounded persistent state. No shared AstrBot tables are modified."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from .core import day, same_topic, topic_key


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state(umo TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, umo TEXT, uid TEXT,
          name TEXT, text TEXT, ts REAL, bot INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS msg_group ON messages(umo,ts);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, umo TEXT, kind TEXT,
          uid TEXT DEFAULT '', name TEXT DEFAULT '', text TEXT DEFAULT '', due REAL,
          expires REAL, created REAL, status TEXT DEFAULT 'pending');
        CREATE INDEX IF NOT EXISTS job_due ON jobs(umo,status,due);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, umo TEXT, date TEXT,
          ts REAL, kind TEXT, text TEXT, content_key TEXT DEFAULT '', status TEXT);
        CREATE INDEX IF NOT EXISTS attempt_group ON attempts(umo,date);
        CREATE TABLE IF NOT EXISTS usage(date TEXT, umo TEXT, calls INTEGER,
          PRIMARY KEY(date,umo));
        CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY, ts REAL,
          kind TEXT, status TEXT, payload TEXT DEFAULT '{}');
        """)
        # An interrupted send/generation is uncertain: do not repeat it on reload.
        self.db.execute("UPDATE jobs SET status='uncertain' WHERE status='sending'")
        self.db.commit()

    @contextmanager
    def transaction(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def state(self, umo):
        row = self.db.execute("SELECT data FROM state WHERE umo=?", (umo,)).fetchone()
        return json.loads(row[0]) if row else {}

    def update(self, umo, **changes):
        data = self.state(umo)
        data.update(changes)
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (umo, json.dumps(data)))
        self.db.commit()

    def message(self, umo, uid, name, text, ts, bot=False):
        self.db.execute("INSERT INTO messages(umo,uid,name,text,ts,bot) VALUES(?,?,?,?,?,?)",
                        (umo, uid, name[:40], text[:500], ts, int(bot)))
        self.db.execute("DELETE FROM messages WHERE umo=? AND id NOT IN "
                        "(SELECT id FROM messages WHERE umo=? ORDER BY id DESC LIMIT 120)", (umo, umo))
        self.db.commit()
        if bot:
            self.update(umo, last_bot=ts)
        else:
            s = self.state(umo)
            changes = {"last_human": ts, "last_seen": ts}
            if s.get("awaiting", 0) and ts - s["awaiting"] <= 3600:
                changes.update(awaiting=0, unanswered=0)
            # Resume after a real person returns following an automatic quiet pause.
            if s.get("auto_pause", 0):
                changes.update(auto_pause=0, unanswered=0, awaiting=0)
            self.update(umo, **changes)

    def recent(self, umo, since=0, limit=30):
        rows = self.db.execute("SELECT * FROM messages WHERE umo=? AND ts>=? "
                               "ORDER BY id DESC LIMIT ?", (umo, since, limit)).fetchall()
        return [dict(x) for x in reversed(rows)]

    def add_job(self, key, umo, kind, due, expires, ts, uid="", name="", text=""):
        cur = self.db.execute("INSERT OR IGNORE INTO jobs(id,umo,kind,uid,name,text,due,expires,created) "
                              "VALUES(?,?,?,?,?,?,?,?,?)", (key, umo, kind, uid, name[:40], text[:500], due, expires, ts))
        self.db.commit()
        return bool(cur.rowcount)

    def add_topic(self, umo, uid, name, kind, text, ts, delay):
        rows = self.db.execute("SELECT text FROM jobs WHERE umo=? AND uid=? AND kind=? AND created>?",
                               (umo, uid, kind, ts - 86400)).fetchall()
        if any(same_topic(r[0], text) for r in rows):
            return False
        pending = self.db.execute("SELECT COUNT(*) FROM jobs WHERE umo=? AND status='pending' AND kind!='share'",
                                  (umo,)).fetchone()[0]
        if pending >= 12:
            return False
        ttl = {"problem": 12 * 3600, "social": 4 * 3600, "purchase": 5400}[kind]
        return self.add_job(topic_key(umo, uid, kind, text, day(ts)), umo, kind,
                            ts + delay, ts + ttl, ts, uid, name, text)

    def cancel_topics(self, umo, uid, text):
        rows = self.db.execute("SELECT id,uid,text FROM jobs WHERE umo=? AND status='pending' AND kind!='share'",
                               (umo,)).fetchall()
        for row in rows:
            if row["uid"] == uid or same_topic(row["text"], text):
                self.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (row["id"],))
        self.db.commit()

    def due(self, umo, ts):
        self.db.execute("UPDATE jobs SET status='expired' WHERE status='pending' AND expires<?", (ts,))
        self.db.commit()
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM jobs WHERE umo=? AND status='pending' AND due<=? AND expires>=? "
            "ORDER BY CASE kind WHEN 'problem' THEN 0 WHEN 'social' THEN 1 WHEN 'purchase' THEN 2 ELSE 3 END,due LIMIT 8",
            (umo, ts, ts))]

    def status(self, key):
        row = self.db.execute("SELECT status FROM jobs WHERE id=?", (key,)).fetchone()
        return row[0] if row else None

    def finish(self, key, status):
        self.db.execute("UPDATE jobs SET status=? WHERE id=? AND status='pending'", (status, key))
        self.db.commit()

    def count(self, umo, ts, manual=None):
        # Includes uncertain/failed sends to avoid retry storms and duplicate deliveries.
        # Existing manual IDs are sufficient to split budgets without rewriting history.
        scope = "" if manual is None else " AND (id GLOB 'manual:*')=?"
        args = (umo, day(ts)) if manual is None else (umo, day(ts), int(manual))
        return self.db.execute("SELECT COUNT(*) FROM attempts WHERE umo=? AND date=?" + scope, args).fetchone()[0]

    def reserve_call(self, umo, ts, group_cap, global_cap):
        with self.transaction():
            local = self.calls(umo, ts)
            total = self.db.execute("SELECT COALESCE(SUM(calls),0) FROM usage WHERE date=?", (day(ts),)).fetchone()[0]
            if local >= group_cap or total >= global_cap:
                return False
            self.db.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(date,umo) DO UPDATE SET calls=calls+1",
                            (day(ts), umo))
        return True

    def calls(self, umo, ts):
        row = self.db.execute("SELECT calls FROM usage WHERE date=? AND umo=?", (day(ts), umo)).fetchone()
        return row[0] if row else 0

    def total_calls(self, ts):
        return self.db.execute("SELECT COALESCE(SUM(calls),0) FROM usage WHERE date=?", (day(ts),)).fetchone()[0]

    def seen(self, umo, content_key, ts):
        return bool(self.db.execute("SELECT 1 FROM attempts WHERE umo=? AND content_key=? AND ts>?",
                                    (umo, content_key, ts - 30 * 86400)).fetchone())

    def reserve_send(self, job, ts, cap, text, content_key=""):
        with self.transaction():
            if self.count(job["umo"], ts, manual=job["id"].startswith("manual:")) >= cap or self.status(job["id"]) != "pending":
                return False
            self.db.execute("UPDATE jobs SET status='sending' WHERE id=?", (job["id"],))
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                            (job["id"], job["umo"], day(ts), ts, job["kind"], text, content_key, "reserved"))
        return True

    def sent(self, key, status):
        self.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, key))
        self.db.execute("UPDATE attempts SET status=? WHERE id=?", (status, key))
        self.db.commit()

    def own_messages(self, umo, ts):
        return [dict(x) for x in self.db.execute("SELECT text,ts FROM attempts WHERE umo=? AND status='sent' "
                                                "AND ts>? ORDER BY ts DESC LIMIT 3", (umo, ts - 6 * 3600))]

    def media_cached(self, ts, interval):
        row = self.db.execute("SELECT * FROM media ORDER BY id DESC LIMIT 1").fetchone()
        if not row or row["ts"] < ts - interval:
            return None
        return dict(row)

    def media_reserve(self, ts, kind):
        cur = self.db.execute("INSERT INTO media(ts,kind,status) VALUES(?,?,'reserved')", (ts, kind))
        self.db.commit()
        return cur.lastrowid

    def media_finish(self, mid, status, payload):
        self.db.execute("UPDATE media SET status=?,payload=? WHERE id=?", (status, json.dumps(payload), mid))
        self.db.commit()

    def prune(self, ts):
        self.db.execute("DELETE FROM messages WHERE ts<?", (ts - 2 * 86400,))
        self.db.execute("DELETE FROM jobs WHERE created<?", (ts - 32 * 86400,))
        self.db.execute("DELETE FROM attempts WHERE ts<?", (ts - 32 * 86400,))
        self.db.execute("DELETE FROM usage WHERE date<?", (day(ts - 32 * 86400),))
        self.db.execute("DELETE FROM media WHERE ts<?", (ts - 32 * 86400,))
        self.db.commit()

    def close(self):
        self.db.close()
