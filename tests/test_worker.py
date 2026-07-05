"""Worker loop resilience: a job reaped mid-run (lease expired, requeued or
retried elsewhere) must not kill the loop — the late result is obsolete and
gets dropped (stats['lost_lease']), observed in the first real Brelje run
where a 24-minute MDO outlived its lease and the stale `running -> failed`
transition crashed the worker."""

from have_agent import connect, transition
from have_agent.executor import ExecResult
from have_agent.substrate import register_worker
from have_agent.worker import Worker
from tests.conftest import force_job, job_row


class ReapedMidRunExecutor:
    """Simulates the reaper winning while the solve is still going: flips the
    job back to queued (as reap_expired does) before returning a result."""

    def __init__(self, db_path, ok=True):
        self.db_path = db_path
        self.ok = ok

    def execute(self, payload, *, study_id, job_id, attempt):
        reaper = connect(self.db_path)
        try:
            transition(
                reaper, job_id, "queued", "system:reaper",
                {"reason": "lease_expired", "exhausted": False},
                expected_state="running",
            )
        finally:
            reaper.close()
        return ExecResult(ok=self.ok, run_ref="run-stale", error=None if self.ok else "boom")


def _run_reaped(tmp_path, conn, study_id, ok):
    db = tmp_path / "muroc.db"  # same file the conn fixture uses
    register_worker(conn, "worker:w1", {"solvers": ["ocp"], "mem_mb": 8192})
    job_id = force_job(conn, study_id, state="queued")
    worker = Worker(db, "worker:w1", ReapedMidRunExecutor(db, ok=ok), poll_s=0.01)
    stats = worker.run(max_jobs=1)
    return job_id, stats


def test_worker_survives_reap_on_success_path(tmp_path, conn, study_id):
    job_id, stats = _run_reaped(tmp_path, conn, study_id, ok=True)
    assert stats["lost_lease"] == 1
    assert stats["succeeded"] == 0 and stats["failed"] == 0
    # the reaper's requeue stands; the stale result did not touch the job
    assert job_row(conn, job_id)["state"] == "queued"


def test_worker_survives_reap_on_failure_path(tmp_path, conn, study_id):
    job_id, stats = _run_reaped(tmp_path, conn, study_id, ok=False)
    # the stale `running -> failed` transition is dropped the same way
    assert stats["lost_lease"] == 1
    assert stats["succeeded"] == 0 and stats["failed"] == 0
    assert job_row(conn, job_id)["state"] == "queued"
