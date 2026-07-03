"""TRIAGE outcomes: policy-bounded auto-retry, beyond-policy proposed retry,
permanent -> infeasible, exhausted -> escalated, dep repointing."""

import json

from have_agent import transition
from have_agent.triage import run_triage
from tests.conftest import events_for, force_job, job_row


def _failed_parent(conn, study_id, worker_id, *, attempt=1, permanent=False, max_attempts=3):
    parent = force_job(
        conn, study_id, state="running", worker=worker_id, attempt=attempt,
        max_attempts=max_attempts,
        payload={"plan_ref": "ocp/t", "case_id": "e300_r300", "overrides": {},
                 "warm_start_run": None},
    )
    transition(conn, parent, "failed", worker_id,
               {"error": "solver diverged", "permanent": permanent})
    transition(conn, parent, "triage", "agent:have")
    triage = force_job(
        conn, study_id, state="running", worker=worker_id, job_type="TRIAGE",
        payload={"failed_job_id": parent, "context": {}},
    )
    return parent, job_row(conn, triage)


def test_retry_within_policy_is_auto_approved(conn, study_id, worker_id):
    parent, triage = _failed_parent(conn, study_id, worker_id, attempt=1)
    result = run_triage(conn, triage)
    assert result["outcome"] == "retry"
    assert result["auto_approved"] is True
    retry = job_row(conn, result["retry_job_id"])
    assert retry["state"] == "approved"
    assert retry["attempt"] == 2
    assert retry["parent_job_id"] == parent
    assert job_row(conn, parent)["state"] == "retry_spawned"
    assert events_for(conn, parent)[-1]["verb"] == "decision.logged"


def test_retry_beyond_policy_waits_for_human(conn, study_id, worker_id):
    # auto_retry_max=2 (study fixture): the retry following attempt 3 is #3
    parent, triage = _failed_parent(conn, study_id, worker_id, attempt=3, max_attempts=5)
    result = run_triage(conn, triage)
    assert result["outcome"] == "retry"
    assert result["auto_approved"] is False
    assert job_row(conn, result["retry_job_id"])["state"] == "proposed"


def test_permanent_failure_is_infeasible(conn, study_id, worker_id):
    parent, triage = _failed_parent(conn, study_id, worker_id, permanent=True)
    result = run_triage(conn, triage)
    assert result["outcome"] == "infeasible"
    assert job_row(conn, parent)["state"] == "infeasible"
    assert events_for(conn, parent)[-2]["verb"] == "job.infeasible"


def test_exhausted_attempts_escalate(conn, study_id, worker_id):
    parent, triage = _failed_parent(conn, study_id, worker_id, attempt=3, max_attempts=3)
    result = run_triage(conn, triage)
    assert result["outcome"] == "escalated"
    assert job_row(conn, parent)["state"] == "escalated"


def test_retry_repoints_dependents(conn, study_id, worker_id):
    parent, triage = _failed_parent(conn, study_id, worker_id)
    check = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[parent])
    result = run_triage(conn, triage)
    deps = conn.execute(
        "SELECT depends_on FROM job_dep WHERE job_id = ?", (check,)
    ).fetchall()
    assert [d["depends_on"] for d in deps] == [result["retry_job_id"]]


def test_retry_warm_starts_from_converged_sibling(conn, study_id, worker_id):
    sibling = force_job(conn, study_id, state="accepted")
    conn.execute("UPDATE job SET run_ref = 'run_ok_1' WHERE id = ?", (sibling,))
    _, triage = _failed_parent(conn, study_id, worker_id)
    # study fixture policy lacks retry_strategy; set it
    conn.execute(
        "UPDATE study SET policy_json = ? WHERE id = ?",
        (json.dumps({"auto_retry_max": 2,
                     "retry_strategy": "warm_start_nearest_converged"}), study_id),
    )
    result = run_triage(conn, triage)
    retry_payload = json.loads(job_row(conn, result["retry_job_id"])["payload_json"])
    assert retry_payload["warm_start_run"] == "run_ok_1"
