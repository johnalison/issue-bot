"""
SQLite-backed state management for the issue bot.
Tracks processed issues, active jobs, and Claude session IDs.
"""

import sqlite3
import contextlib
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).parent / "state.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_issues (
                repo        TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                processed_at TEXT NOT NULL,
                status      TEXT NOT NULL,  -- mr_open, completed, failed, skipped, timeout
                pr_url      TEXT,
                session_id  TEXT,           -- Claude session ID for --resume
                worktree_path TEXT,         -- kept until issue is closed on GitLab
                PRIMARY KEY (repo, issue_number)
            );

            CREATE TABLE IF NOT EXISTS active_jobs (
                repo        TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                started_at  TEXT NOT NULL,
                worktree_path TEXT,
                PRIMARY KEY (repo, issue_number)
            );

            CREATE TABLE IF NOT EXISTS poll_state (
                repo        TEXT PRIMARY KEY,
                last_polled_at TEXT NOT NULL
            );
        """)


def is_processed(repo: str, issue_number: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_issues WHERE repo=? AND issue_number=?",
            (repo, issue_number)
        ).fetchone()
        return row is not None


def is_active(repo: str, issue_number: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM active_jobs WHERE repo=? AND issue_number=?",
            (repo, issue_number)
        ).fetchone()
        return row is not None


def claim_job(repo: str, issue_number: int, worktree_path: str) -> bool:
    """Atomically claim a job. Returns False if already claimed."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO active_jobs (repo, issue_number, started_at, worktree_path) VALUES (?,?,?,?)",
                (repo, issue_number, _now(), worktree_path)
            )
        return True
    except sqlite3.IntegrityError:
        return False


def release_job(repo: str, issue_number: int, status: str,
                pr_url: str = None, session_id: str = None, worktree_path: str = None):
    """Move job from active to processed."""
    with _connect() as conn:
        conn.execute("DELETE FROM active_jobs WHERE repo=? AND issue_number=?",
                     (repo, issue_number))
        conn.execute(
            """INSERT OR REPLACE INTO processed_issues
               (repo, issue_number, processed_at, status, pr_url, session_id, worktree_path)
               VALUES (?,?,?,?,?,?,?)""",
            (repo, issue_number, _now(), status, pr_url, session_id, worktree_path)
        )


def get_open_mr_jobs() -> list[dict]:
    """Return all jobs with status 'mr_open' that still have a worktree to clean up."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM processed_issues WHERE status='mr_open' AND worktree_path IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_completed(repo: str, issue_number: int):
    """Mark a resolved issue as completed and clear the worktree path."""
    with _connect() as conn:
        conn.execute(
            "UPDATE processed_issues SET status='completed', worktree_path=NULL WHERE repo=? AND issue_number=?",
            (repo, issue_number)
        )


def get_last_polled(repo: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_polled_at FROM poll_state WHERE repo=?", (repo,)
        ).fetchone()
        return row["last_polled_at"] if row else None


def set_last_polled(repo: str, ts: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO poll_state (repo, last_polled_at) VALUES (?,?)",
            (repo, ts)
        )


def stale_active_jobs(max_age_seconds: int = 900):
    """Return active jobs older than max_age_seconds (for crash recovery)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM active_jobs").fetchall()
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    stale = []
    for row in rows:
        started = datetime.fromisoformat(row["started_at"]).timestamp()
        if started < cutoff:
            stale.append(dict(row))
    return stale


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
