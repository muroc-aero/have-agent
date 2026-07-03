import sqlite3

import pytest

from have_agent import connect, migrate
from tests.conftest import force_job, force_study


def table_names(conn):
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_migration_creates_substrate_tables(conn):
    assert {"study", "job", "job_dep", "worker", "verdict", "event"} <= table_names(conn)


def test_migration_is_idempotent(tmp_path):
    c = connect(tmp_path / "muroc.db")
    assert migrate(c) == ["0001_substrate.sql", "0002_event_append_only.sql"]
    assert migrate(c) == []
    c.close()


def test_event_is_append_only(conn, study_id):
    event_id = conn.execute(
        "SELECT id FROM event WHERE object_id = ?", (study_id,)
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE event SET verb = 'study.closed' WHERE id = ?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event WHERE id = ?", (event_id,))


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_indexes(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"idx_job_sched", "idx_job_study", "idx_event_object", "idx_event_ts"} <= names


def test_job_state_check_constraint(conn, study_id):
    with pytest.raises(sqlite3.IntegrityError):
        force_job(conn, study_id, state="nonsense")


def test_study_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        force_study(conn, status="nonsense")


def test_job_type_check_constraint(conn, study_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job (id, study_id, type, payload_json, created_at, state_updated_at)"
            " VALUES ('j1', ?, 'BOGUS', '{}', 't', 't')",
            (study_id,),
        )


def test_job_requires_existing_study(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job (id, study_id, type, payload_json, created_at, state_updated_at)"
            " VALUES ('j1', 'no-such-study', 'ANALYSIS', '{}', 't', 't')"
        )


def test_job_dep_primary_key(conn, study_id):
    a = force_job(conn, study_id)
    b = force_job(conn, study_id)
    conn.execute("INSERT INTO job_dep (job_id, depends_on) VALUES (?, ?)", (a, b))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job_dep (job_id, depends_on) VALUES (?, ?)", (a, b))


def test_verdict_level_check_constraint(conn, study_id):
    job = force_job(conn, study_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO verdict (id, job_id, level, checks_json, created_at)"
            " VALUES ('v1', ?, 'meh', '[]', 't')",
            (job,),
        )


def test_event_object_type_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event (id, ts, actor, verb, object_type, object_id)"
            " VALUES ('e1', 't', 'human:alex', 'job.approved', 'plane', 'x')"
        )
