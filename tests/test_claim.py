"""Claim protocol: atomic claim-with-lease, heartbeat extension, reaper —
including two concurrent fake workers hammering one DB."""

import threading
from datetime import UTC, datetime

from have_agent import connect, register_worker, transition
from have_agent.scheduler import claim_next, heartbeat, reap_expired
from tests.conftest import events_for, force_job, job_row


def _register(conn, wid, capacity=1, solvers=("ocp",)):
    register_worker(conn, wid, {"solvers": list(solvers), "mem_mb": 8192}, capacity=capacity)


def _queued(conn, study_id, n=1, **kw):
    return [force_job(conn, study_id, state="queued", **kw) for _ in range(n)]


def test_claim_assigns_worker_and_future_lease(conn, study_id):
    _register(conn, "worker:a")
    (job_id,) = _queued(conn, study_id)
    row = claim_next(conn, "worker:a")
    assert row["id"] == job_id
    assert row["state"] == "assigned"
    assert row["assigned_worker"] == "worker:a"
    lease = datetime.fromisoformat(row["lease_expires_at"])
    # min lease is 10 min (est_runtime_s absent -> minimum applies)
    assert (lease - datetime.now(UTC)).total_seconds() > 590
    assert events_for(conn, job_id)[-1]["verb"] == "job.claimed"


def test_claim_respects_capacity(conn, study_id):
    _register(conn, "worker:a", capacity=1)
    _queued(conn, study_id, 2)
    assert claim_next(conn, "worker:a") is not None
    assert claim_next(conn, "worker:a") is None  # already at capacity


def test_claim_nothing_queued(conn, study_id):
    _register(conn, "worker:a")
    assert claim_next(conn, "worker:a") is None


def test_offline_worker_cannot_claim(conn, study_id):
    _register(conn, "worker:a")
    conn.execute("UPDATE worker SET status = 'draining' WHERE id = 'worker:a'")
    _queued(conn, study_id)
    assert claim_next(conn, "worker:a") is None


def test_two_workers_hammering_one_db(tmp_path, conn, study_id):
    """The step-3 acceptance test: 2 workers, 200 jobs, no double-claims."""
    n_jobs = 200
    jobs = set(_queued(conn, study_id, n_jobs))
    db_path = tmp_path / "muroc.db"  # same file the conn fixture uses
    _register(conn, "worker:a", capacity=n_jobs)
    _register(conn, "worker:b", capacity=n_jobs)

    claims: dict[str, list[str]] = {"worker:a": [], "worker:b": []}
    errors: list[Exception] = []

    def hammer(wid: str):
        c = connect(db_path)
        try:
            while True:
                row = claim_next(c, wid)
                if row is None:
                    break
                claims[wid].append(row["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=hammer, args=(w,)) for w in claims]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    a, b = set(claims["worker:a"]), set(claims["worker:b"])
    assert a & b == set(), "a job was claimed by both workers"
    assert a | b == jobs, "every job claimed exactly once"
    # each claim produced exactly one job.claimed event
    n_events = conn.execute(
        "SELECT COUNT(*) FROM event WHERE verb = 'job.claimed'"
    ).fetchone()[0]
    assert n_events == n_jobs


def test_heartbeat_extends_lease_and_touches_worker(conn, study_id):
    _register(conn, "worker:a")
    job_id = force_job(conn, study_id, state="running", worker="worker:a")
    conn.execute(
        "UPDATE job SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,),
    )
    old_hb = conn.execute(
        "SELECT last_heartbeat FROM worker WHERE id = 'worker:a'"
    ).fetchone()[0]
    assert heartbeat(conn, "worker:a") == 1
    row = job_row(conn, job_id)
    assert row["lease_expires_at"] > "2026"
    new_hb = conn.execute(
        "SELECT last_heartbeat FROM worker WHERE id = 'worker:a'"
    ).fetchone()[0]
    assert new_hb >= old_hb


def test_reaper_requeues_expired_and_increments_attempt(conn, study_id):
    _register(conn, "worker:a")
    job_id = force_job(conn, study_id, state="running", worker="worker:a", attempt=1)
    conn.execute(
        "UPDATE job SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,),
    )
    assert reap_expired(conn) == [(job_id, "queued")]
    row = job_row(conn, job_id)
    assert row["state"] == "queued"
    assert row["attempt"] == 2
    assert row["assigned_worker"] is None
    assert events_for(conn, job_id)[-1]["verb"] == "job.lease_expired"


def test_reaper_fails_exhausted_jobs(conn, study_id):
    _register(conn, "worker:a")
    for state in ("running", "assigned"):
        job_id = force_job(conn, study_id, state=state, worker="worker:a", attempt=3)
        conn.execute(
            "UPDATE job SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )
        assert (job_id, "failed") in reap_expired(conn)
        assert job_row(conn, job_id)["state"] == "failed"
        assert events_for(conn, job_id)[-1]["verb"] == "job.failed"


def test_reaper_ignores_live_leases(conn, study_id):
    _register(conn, "worker:a")
    force_job(conn, study_id, state="running", worker="worker:a")  # lease 9999-...
    assert reap_expired(conn) == []


def test_lease_expired_job_can_be_reclaimed(conn, study_id):
    _register(conn, "worker:a")
    _register(conn, "worker:b")
    (job_id,) = _queued(conn, study_id)
    claim_next(conn, "worker:a")
    transition(conn, job_id, "running", "worker:a")
    conn.execute(
        "UPDATE job SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,),
    )
    reap_expired(conn)
    row = claim_next(conn, "worker:b")
    assert row["id"] == job_id
    assert row["assigned_worker"] == "worker:b"
    assert row["attempt"] == 2
