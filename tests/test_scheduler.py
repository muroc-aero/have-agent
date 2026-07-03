"""Runnable-job query (dep semantics incl. the REPORT any-terminal rule) and
the GreedyPriority policy."""

import json

import pytest

from have_agent.scheduler import (
    Assignment,
    GreedyPriority,
    JobInfo,
    WorkerSlot,
    fits,
    lease_duration_s,
    runnable_jobs,
)
from tests.conftest import force_job

AUTO_ACCEPT_POLICY = {"auto_accept": {"verdict_level": "pass"}}


def _ids(jobs):
    return [j.id for j in jobs]


def set_policy(conn, study_id, policy):
    conn.execute(
        "UPDATE study SET policy_json = ? WHERE id = ?", (json.dumps(policy), study_id)
    )


def test_no_deps_runnable(conn, study_id):
    job = force_job(conn, study_id, state="queued")
    assert _ids(runnable_jobs(conn)) == [job]


def test_non_queued_never_runnable(conn, study_id):
    for state in ("proposed", "approved", "running", "succeeded", "accepted"):
        force_job(conn, study_id, state=state)
    assert runnable_jobs(conn) == []


def test_dep_on_accepted_satisfied(conn, study_id):
    up = force_job(conn, study_id, state="accepted")
    job = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[up])
    assert _ids(runnable_jobs(conn)) == [job]


@pytest.mark.parametrize("dep_state", ["queued", "running", "failed", "review"])
def test_dep_not_satisfied(conn, study_id, dep_state):
    up = force_job(conn, study_id, state=dep_state)
    check = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[up])
    assert check not in _ids(runnable_jobs(conn))


def test_dep_on_succeeded_requires_auto_accept_policy(conn, study_id):
    """§2.2: succeeded satisfies iff the study policy auto-accepts — this is
    what lets a CHECK run before its ANALYSIS is formally accepted."""
    up = force_job(conn, study_id, state="succeeded")
    job = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[up])
    set_policy(conn, study_id, {})
    assert runnable_jobs(conn) == []
    set_policy(conn, study_id, AUTO_ACCEPT_POLICY)
    assert _ids(runnable_jobs(conn)) == [job]


@pytest.mark.parametrize(
    "dep_state", ["accepted", "rejected", "infeasible", "cancelled", "retry_spawned"]
)
def test_report_dep_any_terminal(conn, study_id, dep_state):
    """The report must cover failures too."""
    up = force_job(conn, study_id, state=dep_state)
    job = force_job(conn, study_id, state="queued", job_type="REPORT", deps=[up])
    assert _ids(runnable_jobs(conn)) == [job]


def test_report_dep_exhausted_failed_is_terminal(conn, study_id):
    up = force_job(conn, study_id, state="failed", attempt=3, max_attempts=3)
    job = force_job(conn, study_id, state="queued", job_type="REPORT", deps=[up])
    assert _ids(runnable_jobs(conn)) == [job]


@pytest.mark.parametrize("dep_state", ["succeeded", "running", "triage", "escalated"])
def test_report_dep_non_terminal_blocks(conn, study_id, dep_state):
    up = force_job(conn, study_id, state=dep_state)
    force_job(conn, study_id, state="queued", job_type="REPORT", deps=[up])
    assert runnable_jobs(conn) == []


def test_report_dep_retryable_failed_blocks(conn, study_id):
    up = force_job(conn, study_id, state="failed", attempt=1, max_attempts=3)
    force_job(conn, study_id, state="queued", job_type="REPORT", deps=[up])
    assert runnable_jobs(conn) == []


def test_all_deps_must_be_satisfied(conn, study_id):
    ok = force_job(conn, study_id, state="accepted")
    pending = force_job(conn, study_id, state="running")
    force_job(conn, study_id, state="queued", job_type="REPORT", deps=[ok, pending])
    assert runnable_jobs(conn) == []


def test_priority_then_fifo_order(conn, study_id):
    low = force_job(conn, study_id, state="queued", priority=90)
    first = force_job(conn, study_id, state="queued", priority=10)
    second = force_job(conn, study_id, state="queued", priority=10)
    assert _ids(runnable_jobs(conn)) == [first, second, low]


# --- GreedyPriority ---------------------------------------------------------


def _job(i, requires=(), mem=None, priority=50):
    resource = {"requires": list(requires)}
    if mem:
        resource["mem_mb"] = mem
    return JobInfo(
        id=f"j{i}", study_id="s", type="ANALYSIS", priority=priority,
        created_at=str(i), resource=resource, attempt=1, max_attempts=3,
    )


def _slot(wid, solvers=("ocp",), mem=8192, free=1):
    return WorkerSlot(wid, {"solvers": list(solvers), "mem_mb": mem}, free)


def test_fits_solver_and_memory():
    assert fits(_job(1, requires=["ocp"]), _slot("w"))
    assert not fits(_job(1, requires=["oas"]), _slot("w"))
    assert not fits(_job(1, mem=16384), _slot("w", mem=8192))


def test_greedy_first_fit_respects_order_and_slots():
    jobs = [_job(1), _job(2), _job(3)]
    picks = GreedyPriority().select(jobs, [_slot("a", free=2), _slot("b", free=1)])
    assert picks == [
        Assignment("j1", "a"), Assignment("j2", "a"), Assignment("j3", "b"),
    ]


def test_greedy_skips_unfit_jobs():
    jobs = [_job(1, requires=["oas"]), _job(2)]
    picks = GreedyPriority().select(jobs, [_slot("a")])
    assert picks == [Assignment("j2", "a")]


def test_lease_duration():
    assert lease_duration_s({}) == 600  # min 10 min
    assert lease_duration_s({"est_runtime_s": 100}) == 600
    assert lease_duration_s({"est_runtime_s": 1000}) == 2000  # 2x estimate
