import json
import sqlite3
import time
import uuid
from pathlib import Path

HOUR = 3600


class SessionError(ValueError):
    pass


class Store:

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(
                parents=True,
                exist_ok=True
            )

        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")

        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS copies (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                session_id TEXT NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
                key TEXT PRIMARY KEY,
                until REAL NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def normalize(self, session):
        session.setdefault(
            "detected",
            dict(session.get("attendees", {}))
        )

        session.setdefault("manual_added", {})
        session.setdefault("manual_removed", [])
        session.setdefault("attendees", {})

        return session

    def save(self, session):
        with self.db:
            self.db.execute(
                """
                INSERT INTO sessions VALUES (?, ?)
                ON CONFLICT(id)
                DO UPDATE SET payload=excluded.payload
                """,
                (
                    session["id"],
                    json.dumps(session)
                )
            )

    def get(self, sid):
        row = self.db.execute(
            "SELECT payload FROM sessions WHERE id=?",
            (sid,)
        ).fetchone()

        if row is None:
            raise SessionError(
                "This player list is no longer available."
            )

        return self.normalize(
            json.loads(row["payload"])
        )

    def all(self):
        return [
            self.normalize(json.loads(row["payload"]))
            for row in self.db.execute(
                "SELECT payload FROM sessions"
            )
        ]

    def create(
        self,
        guild_id,
        game,
        game_name,
        host_id,
        host_name,
        recipients,
        join_url,
        join_note,
        now=None
    ):
        now = time.time() if now is None else now
        sid = uuid.uuid4().hex

        session = {
            "id": sid,
            "guild_id": str(guild_id),
            "game": game,
            "game_name": game_name,
            "host_id": str(host_id),
            "host_name": host_name,
            "recipients": recipients,

            "detected": {},
            "manual_added": {},
            "manual_removed": [],
            "attendees": {},

            "created_at": now,
            "expires_at": now + HOUR,

            "locked": False,
            "revision": 1,

            "join_url": join_url,
            "join_note": join_note
        }

        self.save(session)

        return session

    def delete(self, sid):
        with self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE id=?",
                (sid,)
            )

    def rebuild_attendees(self, session):
        players = dict(session["detected"])

        players.update(
            session["manual_added"]
        )

        for uid in session["manual_removed"]:
            players.pop(uid, None)

        return dict(
            sorted(
                players.items(),
                key=lambda item: (
                    item[1].casefold(),
                    item[0]
                )
            )
        )

    def set_detected(self, sid, detected):
        session = self.get(sid)

        if session["locked"]:
            return session

        detected = {
            str(uid): str(name)
            for uid, name in detected.items()
        }

        if session["detected"] == detected:
            return session

        before = dict(session["attendees"])

        session["detected"] = detected

        session["attendees"] = (
            self.rebuild_attendees(session)
        )

        if before != session["attendees"]:
            session["revision"] += 1

        self.save(session)

        return session

    def set_overrides(self, sid, names, added):
        session = self.get(sid)

        if (
            session["locked"]
            or time.time() >= session["expires_at"]
        ):
            raise SessionError(
                "This hop session is closed."
            )

        before = dict(session["attendees"])

        manual_added = dict(
            session["manual_added"]
        )

        manual_removed = set(
            session["manual_removed"]
        )

        for uid, name in names.items():
            uid = str(uid)

            if added:
                manual_added[uid] = str(name)
                manual_removed.discard(uid)

            else:
                manual_added.pop(uid, None)
                manual_removed.add(uid)

        session["manual_added"] = manual_added

        session["manual_removed"] = sorted(
            manual_removed
        )

        session["attendees"] = (
            self.rebuild_attendees(session)
        )

        if before != session["attendees"]:
            session["revision"] += 1

        self.save(session)

        return session

    def set_population(self, sid, population):
        session = self.get(sid)

        if session.get("population") != population:
            session["population"] = population
            session["revision"] += 1

            self.save(session)

        return session

    def finish(self, sid):
        session = self.get(sid)

        if not session["locked"]:
            session["locked"] = True
            session["revision"] += 1

            self.save(session)

        return session

    def expire(self, now=None):
        now = time.time() if now is None else now

        for session in self.all():
            if (
                not session["locked"]
                and now >= session["expires_at"]
            ):
                self.finish(session["id"])

    def add_copy(
        self,
        sid,
        channel_id,
        message_id,
        revision
    ):
        with self.db:
            self.db.execute(
                "INSERT INTO copies VALUES (?, ?, ?, ?)",
                (
                    str(message_id),
                    str(channel_id),
                    sid,
                    revision
                )
            )

    def copies(self, sid):
        return list(
            self.db.execute(
                "SELECT * FROM copies WHERE session_id=?",
                (sid,)
            )
        )

    def invalidate_copies(self):
        with self.db:
            self.db.execute(
                "UPDATE copies SET revision=0"
            )

    def mark_copy(self, message_id, revision):
        with self.db:
            self.db.execute(
                "UPDATE copies SET revision=? WHERE message_id=?",
                (revision, str(message_id))
            )

    def remove_copy(self, message_id):
        with self.db:
            self.db.execute(
                "DELETE FROM copies WHERE message_id=?",
                (str(message_id),)
            )

    def reserve_cooldown(
        self,
        key,
        seconds,
        now=None
    ):
        if seconds <= 0:
            self.release_cooldown(key)
            return

        now = time.time() if now is None else now

        row = self.db.execute(
            "SELECT until FROM cooldowns WHERE key=?",
            (key,)
        ).fetchone()

        if row and row["until"] > now:
            remaining = int(row["until"] - now) + 1

            raise SessionError(
                f"Try again in {remaining} seconds."
            )

        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cooldowns VALUES (?, ?)",
                (key, now + seconds)
            )

    def release_cooldown(self, key):
        with self.db:
            self.db.execute(
                "DELETE FROM cooldowns WHERE key=?",
                (key,)
            )