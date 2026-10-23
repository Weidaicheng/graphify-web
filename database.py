"""SQLite database layer for Graphify Admin."""

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
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                github_url  TEXT NOT NULL UNIQUE,
                local_path  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'cloning',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS graphs (
                id              TEXT PRIMARY KEY,
                repo_id         TEXT REFERENCES repos(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                graph_json_path TEXT NOT NULL,
                node_count      INTEGER DEFAULT 0,
                link_count      INTEGER DEFAULT 0,
                community_count INTEGER DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'pending',
                error_message   TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                repo_id     TEXT REFERENCES repos(id) ON DELETE CASCADE,
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

    def add_repo(self, name: str, github_url: str, local_path: str) -> dict:
        now = self._now()
        rid = self._uuid()
        self.conn.execute(
            "INSERT INTO repos (id, name, github_url, local_path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'cloning', ?, ?)",
            (rid, name, github_url, local_path, now, now),
        )
        self.conn.commit()
        return self.get_repo(rid)

    def get_repo(self, repo_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return dict(row) if row else None

    def get_repo_by_url(self, github_url: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM repos WHERE github_url = ?", (github_url,)).fetchone()
        return dict(row) if row else None

    def list_repos(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_repo(self, repo_id: str, **fields):
        fields["updated_at"] = self._now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [repo_id]
        self.conn.execute(f"UPDATE repos SET {sets} WHERE id = ?", vals)
        self.conn.commit()

    def delete_repo(self, repo_id: str):
        self.conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        self.conn.commit()

    # ── graphs ───────────────────────────────────────────────

    def add_graph(self, repo_id: str, name: str, graph_json_path: str) -> dict:
        gid = self._uuid()
        self.conn.execute(
            "INSERT INTO graphs (id, repo_id, name, graph_json_path, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (gid, repo_id, name, graph_json_path, self._now()),
        )
        self.conn.commit()
        return self.get_graph(gid)

    def get_graph(self, graph_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM graphs WHERE id = ?", (graph_id,)).fetchone()
        return dict(row) if row else None

    def list_graphs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT g.*, r.name as repo_name, r.github_url "
            "FROM graphs g LEFT JOIN repos r ON g.repo_id = r.id "
            "ORDER BY g.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_graphs_by_repo(self, repo_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM graphs WHERE repo_id = ? ORDER BY created_at DESC", (repo_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_graph(self, graph_id: str, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [graph_id]
        self.conn.execute(f"UPDATE graphs SET {sets} WHERE id = ?", vals)
        self.conn.commit()

    def delete_graph(self, graph_id: str):
        self.conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))
        self.conn.commit()

    # ── jobs ─────────────────────────────────────────────────

    def add_job(self, repo_id: str, job_type: str) -> dict:
        jid = self._uuid()
        self.conn.execute(
            "INSERT INTO jobs (id, repo_id, job_type, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            (jid, repo_id, job_type, self._now()),
        )
        self.conn.commit()
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [job_id]
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", vals)
        self.conn.commit()

    def list_jobs(self, repo_id: str = None, limit: int = 20) -> list[dict]:
        if repo_id:
            rows = self.conn.execute(
                "SELECT j.*, r.name as repo_name FROM jobs j "
                "LEFT JOIN repos r ON j.repo_id = r.id "
                "WHERE j.repo_id = ? ORDER BY j.started_at DESC LIMIT ?",
                (repo_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT j.*, r.name as repo_name FROM jobs j "
                "LEFT JOIN repos r ON j.repo_id = r.id "
                "ORDER BY j.started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── aggregate stats ──────────────────────────────────────

    def get_dashboard_stats(self) -> dict:
        repos = self.conn.execute("SELECT COUNT(*) as c FROM repos").fetchone()["c"]
        graphs = self.conn.execute("SELECT COUNT(*) as c FROM graphs WHERE status = 'ready'").fetchone()["c"]
        nodes = self.conn.execute("SELECT COALESCE(SUM(node_count), 0) as c FROM graphs WHERE status = 'ready'").fetchone()["c"]
        links = self.conn.execute("SELECT COALESCE(SUM(link_count), 0) as c FROM graphs WHERE status = 'ready'").fetchone()["c"]
        return {"repos": repos, "graphs": graphs, "nodes": nodes, "links": links}
