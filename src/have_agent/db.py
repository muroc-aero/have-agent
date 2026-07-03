"""SQLite connection and migration runner for muroc.db."""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


def utcnow() -> str:
    """ISO-8601 UTC timestamp; lexicographic order == chronological order."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: autocommit mode; transactions are explicit
    # (BEGIN IMMEDIATE / COMMIT) so multi-statement writes stay atomic.
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending .sql migrations shipped with the package. Returns names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration"
        " (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migration")}
    migration_dir = resources.files("have_agent") / "migrations"
    done = []
    for entry in sorted(migration_dir.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".sql") or entry.name in applied:
            continue
        conn.executescript(entry.read_text())
        conn.execute(
            "INSERT INTO schema_migration (name, applied_at) VALUES (?, ?)",
            (entry.name, utcnow()),
        )
        done.append(entry.name)
    return done
