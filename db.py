"""SQLite storage and member matching for the DVC community bot."""
import re
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    name         TEXT,
    company      TEXT,
    role         TEXT,
    expertise    TEXT,
    offers       TEXT,
    interests    TEXT,
    looking_for  TEXT,
    searchable   INTEGER DEFAULT 0,
    intro_status TEXT DEFAULT 'none',      -- none | pending | posted | rejected
    created_at   INTEGER,
    updated_at   INTEGER
);
CREATE TABLE IF NOT EXISTS asks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER,
    need             TEXT,
    who              TEXT,
    deadline         TEXT,
    status           TEXT DEFAULT 'pending', -- pending | live | closed | rejected
    outcome          TEXT,                   -- helped | partial | unresolved
    chat_id          INTEGER,
    message_id       INTEGER,
    created_at       INTEGER,
    approved_at      INTEGER,
    closed_at        INTEGER,
    followups        INTEGER DEFAULT 0,
    last_followup_at INTEGER
);
CREATE TABLE IF NOT EXISTS ask_responses (
    ask_id     INTEGER,
    user_id    INTEGER,
    kind       TEXT,                         -- help | same
    created_at INTEGER,
    PRIMARY KEY (ask_id, user_id, kind)
);
CREATE TABLE IF NOT EXISTS submissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,                         -- intro | topic
    user_id    INTEGER,
    text       TEXT,
    anonymous  INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'pending',       -- pending | posted | rejected
    chat_id    INTEGER,
    message_id INTEGER,
    created_at INTEGER,
    decided_at INTEGER
);
"""

STOPWORDS = set("""
a an and any anyone are as at be been but by can could do does for from has have
help i in into is it know looking me my need of on or our someone something that
the their them they this to us want was we who with you your someone's anybody
experience experienced person people member members find
""".split())


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {_stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


# Fields searched, with weights: what someone knows and offers count most.
FIELD_WEIGHTS = {"expertise": 3, "offers": 3, "role": 1, "company": 1, "interests": 1}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _run(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---------- members ----------
    def get_member(self, user_id):
        return self._one("SELECT * FROM members WHERE user_id=?", (user_id,))

    def upsert_member(self, user_id, username, **f):
        now = int(time.time())
        if self.get_member(user_id):
            cols = ", ".join(f"{k}=?" for k in f)
            self._run(f"UPDATE members SET username=?, {cols}, updated_at=? WHERE user_id=?",
                      (username, *f.values(), now, user_id))
        else:
            keys = ["user_id", "username", *f.keys(), "created_at", "updated_at"]
            self._run(f"INSERT INTO members ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                      (user_id, username, *f.values(), now, now))

    def set_member_field(self, user_id, field, value):
        assert field in {"searchable", "intro_status", "username"}
        self._run(f"UPDATE members SET {field}=? WHERE user_id=?", (value, user_id))

    def search_members(self, query, exclude_user_id=None, limit=5):
        q = tokens(query)
        if not q:
            return []
        results = []
        for m in self._all("SELECT * FROM members WHERE searchable=1"):
            if m["user_id"] == exclude_user_id:
                continue
            score, matched = 0, set()
            for field, weight in FIELD_WEIGHTS.items():
                ft = tokens(m[field])
                for qt in q:
                    hit = qt in ft or (len(qt) >= 4 and any(t.startswith(qt) or qt.startswith(t) and len(t) >= 4 for t in ft))
                    if hit:
                        score += weight
                        matched.add(qt)
            if matched:
                # Covering more of the query beats repeating one word.
                results.append((len(matched) * 10 + score, m))
        results.sort(key=lambda r: -r[0])
        return [m for _, m in results[:limit]]

    # ---------- asks ----------
    def create_ask(self, user_id, need, who, deadline):
        return self._run("INSERT INTO asks (user_id, need, who, deadline, created_at) VALUES (?,?,?,?,?)",
                         (user_id, need, who, deadline, int(time.time()))).lastrowid

    def get_ask(self, ask_id):
        return self._one("SELECT * FROM asks WHERE id=?", (ask_id,))

    def asks_for_user(self, user_id, limit=10):
        return self._all("SELECT * FROM asks WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def pending_asks(self):
        return self._all("SELECT * FROM asks WHERE status='pending' ORDER BY id")

    def approve_ask(self, ask_id, chat_id, message_id):
        self._run("UPDATE asks SET status='live', chat_id=?, message_id=?, approved_at=? WHERE id=?",
                  (chat_id, message_id, int(time.time()), ask_id))

    def reject_ask(self, ask_id):
        self._run("UPDATE asks SET status='rejected', closed_at=? WHERE id=?", (int(time.time()), ask_id))

    def close_ask(self, ask_id, outcome):
        self._run("UPDATE asks SET status='closed', outcome=?, closed_at=? WHERE id=?",
                  (outcome, int(time.time()), ask_id))

    def add_response(self, ask_id, user_id, kind) -> bool:
        """Returns True if this is a new response (not a repeat tap)."""
        cur = self._run("INSERT OR IGNORE INTO ask_responses VALUES (?,?,?,?)",
                        (ask_id, user_id, kind, int(time.time())))
        return cur.rowcount == 1

    def response_counts(self, ask_id):
        rows = self._all("SELECT kind, COUNT(*) c FROM ask_responses WHERE ask_id=? GROUP BY kind", (ask_id,))
        counts = {"help": 0, "same": 0}
        counts.update({r["kind"]: r["c"] for r in rows})
        return counts

    def due_followups(self, after_seconds, max_followups=2):
        cutoff = int(time.time() - after_seconds)
        return self._all("""SELECT * FROM asks WHERE status='live' AND followups < ?
                            AND COALESCE(last_followup_at, approved_at) <= ?""", (max_followups, cutoff))

    def mark_followup(self, ask_id):
        self._run("UPDATE asks SET followups=followups+1, last_followup_at=? WHERE id=?",
                  (int(time.time()), ask_id))

    # ---------- intros & discussion topics ----------
    def create_submission(self, kind, user_id, text, anonymous=False):
        return self._run("INSERT INTO submissions (kind, user_id, text, anonymous, created_at) VALUES (?,?,?,?,?)",
                         (kind, user_id, text, int(anonymous), int(time.time()))).lastrowid

    def get_submission(self, sub_id):
        return self._one("SELECT * FROM submissions WHERE id=?", (sub_id,))

    def pending_submissions(self):
        return self._all("SELECT * FROM submissions WHERE status='pending' ORDER BY id")

    def decide_submission(self, sub_id, status, chat_id=None, message_id=None):
        self._run("UPDATE submissions SET status=?, chat_id=?, message_id=?, decided_at=? WHERE id=?",
                  (status, chat_id, message_id, int(time.time()), sub_id))

    # ---------- success measures ----------
    def stats(self):
        s = {}
        s["members"] = self._one("SELECT COUNT(*) FROM members")[0]
        s["searchable"] = self._one("SELECT COUNT(*) FROM members WHERE searchable=1")[0]
        s["intros_posted"] = self._one("SELECT COUNT(*) FROM members WHERE intro_status='posted'")[0]
        for status in ("pending", "live", "closed", "rejected"):
            s[f"asks_{status}"] = self._one("SELECT COUNT(*) FROM asks WHERE status=?", (status,))[0]
        for outcome in ("helped", "partial", "unresolved"):
            s[f"outcome_{outcome}"] = self._one("SELECT COUNT(*) FROM asks WHERE outcome=?", (outcome,))[0]
        s["published_asks"] = s["asks_live"] + s["asks_closed"]
        s["asks_with_offer"] = self._one("""SELECT COUNT(DISTINCT ask_id) FROM ask_responses r
                                            JOIN asks a ON a.id=r.ask_id WHERE r.kind='help'""")[0]
        s["help_offers"] = self._one("SELECT COUNT(*) FROM ask_responses WHERE kind='help'")[0]
        s["distinct_helpers"] = self._one("SELECT COUNT(DISTINCT user_id) FROM ask_responses WHERE kind='help'")[0]
        s["same_question"] = self._one("SELECT COUNT(*) FROM ask_responses WHERE kind='same'")[0]
        s["topics_submitted"] = self._one("SELECT COUNT(*) FROM submissions WHERE kind='topic'")[0]
        s["topics_posted"] = self._one("SELECT COUNT(*) FROM submissions WHERE kind='topic' AND status='posted'")[0]
        # Members who contributed more than once (asks, help offers, or topics).
        s["repeat_contributors"] = self._one("""
            SELECT COUNT(*) FROM (
                SELECT user_id FROM (
                    SELECT user_id FROM asks WHERE status IN ('live','closed')
                    UNION ALL SELECT user_id FROM ask_responses WHERE kind='help'
                    UNION ALL SELECT user_id FROM submissions WHERE kind='topic' AND status='posted'
                ) GROUP BY user_id HAVING COUNT(*) >= 2)""")[0]
        return s
