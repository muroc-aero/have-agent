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


def _enable_warm_start(conn, study_id):
    # study fixture policy lacks retry_strategy; set it
    conn.execute(
        "UPDATE study SET policy_json = ? WHERE id = ?",
        (json.dumps({"auto_retry_max": 2,
                     "retry_strategy": "warm_start_nearest_converged"}), study_id),
    )


def _converged_sibling(conn, study_id, run_ref, case_id, overrides, state="accepted"):
    sibling = force_job(
        conn, study_id, state=state,
        payload={"plan_ref": "ocp/t", "case_id": case_id, "overrides": overrides},
    )
    conn.execute("UPDATE job SET run_ref = ? WHERE id = ?", (run_ref, sibling))
    return sibling


def test_retry_warm_starts_from_converged_sibling(conn, study_id, worker_id):
    sibling = force_job(conn, study_id, state="accepted")
    conn.execute("UPDATE job SET run_ref = 'run_ok_1' WHERE id = ?", (sibling,))
    _, triage = _failed_parent(conn, study_id, worker_id)
    _enable_warm_start(conn, study_id)
    result = run_triage(conn, triage)
    retry_payload = json.loads(job_row(conn, result["retry_job_id"])["payload_json"])
    assert retry_payload["warm_start_run"] == "run_ok_1"


def test_warm_start_picks_nearest_in_sweep(conn, study_id, worker_id):
    # failed case at (450, 650): the sibling one e-step away wins over the
    # same-e sibling a full range-axis away and the far corner. run_near is
    # created first so the old most-recently-finished stub would pick
    # run_same_e — this asserts distance, not recency.
    _converged_sibling(conn, study_id, "run_near", "e500_r650",
                       {"battery.specific_energy_whkg": 500, "mission.range_nm": 650},
                       state="succeeded")
    _converged_sibling(conn, study_id, "run_far", "e250_r300",
                       {"battery.specific_energy_whkg": 250, "mission.range_nm": 300})
    _converged_sibling(conn, study_id, "run_same_e", "e450_r300",
                       {"battery.specific_energy_whkg": 450, "mission.range_nm": 300})
    parent = force_job(
        conn, study_id, state="running", worker=worker_id,
        payload={"plan_ref": "ocp/t", "case_id": "e450_r650",
                 "overrides": {"battery.specific_energy_whkg": 450,
                               "mission.range_nm": 650},
                 "warm_start_run": None},
    )
    transition(conn, parent, "failed", worker_id, {"error": "prop map NaN"})
    transition(conn, parent, "triage", "agent:have")
    triage = force_job(conn, study_id, state="running", worker=worker_id,
                       job_type="TRIAGE", payload={"failed_job_id": parent, "context": {}})
    _enable_warm_start(conn, study_id)

    result = run_triage(conn, job_row(conn, triage))
    assert result["warm_start_run"] == "run_near"
    assert result["warm_start_case"] == "e500_r650"
    retry_payload = json.loads(job_row(conn, result["retry_job_id"])["payload_json"])
    assert retry_payload["warm_start_run"] == "run_near"
    # the pick is audited on the decision.logged event
    decision = json.loads(events_for(conn, parent)[-1]["payload_json"])
    assert decision["warm_start_case"] == "e500_r650"


def test_warm_start_none_converged_stays_cold(conn, study_id, worker_id):
    _, triage = _failed_parent(conn, study_id, worker_id)
    _enable_warm_start(conn, study_id)
    result = run_triage(conn, triage)
    assert "warm_start_run" not in result
    retry_payload = json.loads(job_row(conn, result["retry_job_id"])["payload_json"])
    assert retry_payload["warm_start_run"] is None
