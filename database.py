"""SQLite database layer for Graphify Web."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Database:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS repos (
                owner           TEXT NOT NULL,
                name            TEXT NOT NULL,
                local_path      TEXT NOT NULL,
                graph_json_path TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                node_count      INTEGER DEFAULT 0,
                link_count      INTEGER DEFAULT 0,
                community_count INTEGER DEFAULT 0,
                last_commit_sha TEXT,
                last_built_sha  TEXT,
                error_message   TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (owner, name)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                owner       TEXT NOT NULL,
                name        TEXT NOT NULL,
                job_type    TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'running',
                log_output  TEXT DEFAULT '',
                started_at  TEXT NOT NULL,
                finished_at TEXT
            );
        """)
        self.conn.commit()

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _uuid() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── repos ────────────────────────────────────────────────

    def add_repo(self, owner: str, name: str, local_path: str) -> dict:
        now = self._now()
        self.conn.execute(
            "INSERT OR IGNORE INTO repos "
            "(owner, name, local_path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (owner, name, local_path, now, now),
        )
        self.conn.commit()
        return self.get_repo(owner, name)

    def get_repo(self, owner: str, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM repos WHERE owner = ? AND name = ?", (owner, name)
        ).fetchone()
        return dict(row) if row else None

    def update_repo(self, owner: str, name: str, **fields):
        if not fields:
            return
        fields["updated_at"] = self._now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [owner, name]
        self.conn.execute(
            f"UPDATE repos SET {sets} WHERE owner = ? AND name = ?", vals
        )
        self.conn.commit()

    def delete_repo(self, owner: str, name: str):
        self.conn.execute(
            "DELETE FROM repos WHERE owner = ? AND name = ?", (owner, name)
        )
        self.conn.commit()

    def list_repos(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM repos ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── jobs ─────────────────────────────────────────────────

    def add_job(self, owner: str, name: str, job_type: str) -> dict:
        jid = self._uuid()
        self.conn.execute(
            "INSERT INTO jobs (id, owner, name, job_type, status, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (jid, owner, name, job_type, self._now()),
        )
        self.conn.commit()
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [job_id]
        self.conn.execute(
            f"UPDATE jobs SET {sets} WHERE id = ?", vals
        )
        self.conn.commit()

    def list_jobs(self, owner: str = None, name: str = None, limit: int = 20) -> list[dict]:
        if owner and name:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE owner = ? AND name = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (owner, name, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_job(self, owner: str, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE owner = ? AND name = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (owner, name),
        ).fetchone()
        return dict(row) if row else None
