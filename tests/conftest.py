import json

import pytest

from have_agent import connect, create_study, migrate, register_worker, utcnow
from have_agent.ids import new_ulid

WORKER_ID = "worker:test-1"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "muroc.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def study_id(conn):
    return create_study(
        conn,
        title="test study",
        intent_yaml="study: test\n",
        owner="human:alex",
        policy={
            "auto_accept": {"verdict_level": "pass"},
            "auto_retry_max": 2,
            "gate_on": ["warn", "fail"],
        },
    )


@pytest.fixture
def worker_id(conn):
    register_worker(conn, WORKER_ID, {"solvers": ["ocp"], "mem_mb": 4096})
    return WORKER_ID


def force_job(
    conn,
    study_id,
    state="proposed",
    *,
    worker=None,
    attempt=1,
    job_type="ANALYSIS",
    priority=50,
    resource=None,
    payload=None,
    deps=(),
    max_attempts=3,
):
    """Test-only backdoor: insert a job directly in an arbitrary state.

    Production code must never write job.state outside transition(); tests
    need it to set up every from-state without walking the whole machine.
    """
    job_id = new_ulid()
    now = utcnow()
    leased = state in ("assigned", "running")
    conn.execute(
        "INSERT INTO job (id, study_id, type, state, priority, resource_json, payload_json,"
        " assigned_worker, lease_expires_at, attempt, max_attempts, created_at,"
        " state_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, study_id, job_type, state, priority,
            json.dumps(resource or {}), json.dumps(payload or {"case_id": "t"}),
            worker if leased else None,
            "9999-01-01T00:00:00+00:00" if leased else None,
            attempt, max_attempts, now, now,
        ),
    )
    for dep in deps:
        conn.execute("INSERT INTO job_dep (job_id, depends_on) VALUES (?, ?)", (job_id, dep))
    return job_id


def force_study(conn, status="draft"):
    """Test-only backdoor: insert a study directly in an arbitrary status."""
    sid = new_ulid()
    now = utcnow()
    conn.execute(
        "INSERT INTO study (id, title, intent_yaml, status, owner, policy_json,"
        " created_at, updated_at) VALUES (?, 't', 'y', ?, 'human:alex', '{}', ?, ?)",
        (sid, status, now, now),
    )
    return sid


def job_row(conn, job_id):
    return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()


def study_row(conn, study_id):
    return conn.execute("SELECT * FROM study WHERE id = ?", (study_id,)).fetchone()


def events_for(conn, object_id):
    return conn.execute(
        "SELECT * FROM event WHERE object_id = ? ORDER BY ts, id", (object_id,)
    ).fetchall()
