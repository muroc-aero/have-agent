"""Control tick: enqueue, triage spawn, verdict gates, dead-branch
cancellation, study status sync."""

import json

from have_agent import record_verdict, transition
from have_agent.control import (
    apply_verdict_gates,
    auto_accept_support_jobs,
    cancel_dead_branches,
    enqueue_approved,
    spawn_triage,
    sync_study_status,
)
from tests.conftest import events_for, force_job, force_study, job_row, study_row


def test_enqueue_approved(conn, study_id):
    a = force_job(conn, study_id, state="approved")
    b = force_job(conn, study_id, state="proposed")
    assert enqueue_approved(conn) == 1
    assert job_row(conn, a)["state"] == "queued"
    assert job_row(conn, b)["state"] == "proposed"


def test_spawn_triage_creates_child_and_flips_parent(conn, study_id):
    failed = force_job(conn, study_id, state="failed", priority=50)
    (triage_id,) = spawn_triage(conn)
    assert job_row(conn, failed)["state"] == "triage"
    t = job_row(conn, triage_id)
    assert t["type"] == "TRIAGE"
    assert t["state"] == "approved"  # machine job, auto-approved
    assert t["parent_job_id"] == failed
    assert t["priority"] == 40  # jumps the queue
    assert json.loads(t["payload_json"])["failed_job_id"] == failed
    assert events_for(conn, failed)[-1]["verb"] == "job.triage_started"
    # idempotent: second tick spawns nothing
    assert spawn_triage(conn) == []


def test_spawn_triage_repairs_missing_child(conn, study_id):
    # a triage-state job with no TRIAGE child (crash between flip and create)
    orphan = force_job(conn, study_id, state="triage")
    (triage_id,) = spawn_triage(conn)
    assert job_row(conn, triage_id)["parent_job_id"] == orphan


def _check_with_verdict(conn, study_id, level, analysis_state="succeeded"):
    analysis = force_job(conn, study_id, state=analysis_state)
    check = force_job(
        conn, study_id, state="running", job_type="CHECK", worker="worker:test-1",
        deps=[analysis],
    )
    verdict_id = record_verdict(conn, check, level, [], "worker:test-1")
    transition(conn, check, "succeeded", "worker:test-1", verdict_id=verdict_id)
    return analysis, check


def test_verdict_pass_auto_accepts_analysis_and_check(conn, study_id, worker_id):
    analysis, check = _check_with_verdict(conn, study_id, "pass")
    assert apply_verdict_gates(conn) == 2
    assert job_row(conn, analysis)["state"] == "accepted"
    assert job_row(conn, check)["state"] == "accepted"


def test_verdict_warn_gates_analysis_to_review(conn, study_id, worker_id):
    analysis, check = _check_with_verdict(conn, study_id, "warn")
    apply_verdict_gates(conn)
    assert job_row(conn, analysis)["state"] == "review"
    assert job_row(conn, check)["state"] == "accepted"
    assert events_for(conn, analysis)[-1]["verb"] == "job.review_requested"


def test_verdict_fail_gates_analysis_to_review(conn, study_id, worker_id):
    analysis, _ = _check_with_verdict(conn, study_id, "fail")
    apply_verdict_gates(conn)
    assert job_row(conn, analysis)["state"] == "review"


def test_auto_accept_support_jobs(conn, study_id):
    triage = force_job(conn, study_id, state="succeeded", job_type="TRIAGE")
    report = force_job(conn, study_id, state="succeeded", job_type="REPORT")
    analysis = force_job(conn, study_id, state="succeeded")
    assert auto_accept_support_jobs(conn) == 2
    assert job_row(conn, triage)["state"] == "accepted"
    assert job_row(conn, report)["state"] == "accepted"
    assert job_row(conn, analysis)["state"] == "succeeded"  # gated via CHECK verdict


def test_cancel_dead_branches(conn, study_id):
    dead_up = force_job(conn, study_id, state="infeasible")
    check = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[dead_up])
    report = force_job(conn, study_id, state="queued", job_type="REPORT", deps=[dead_up])
    live_up = force_job(conn, study_id, state="running")
    other = force_job(conn, study_id, state="queued", job_type="CHECK", deps=[live_up])
    assert cancel_dead_branches(conn) == 1
    assert job_row(conn, check)["state"] == "cancelled"
    assert job_row(conn, report)["state"] == "queued"  # REPORT treats it as terminal
    assert job_row(conn, other)["state"] == "queued"


def test_sync_study_approved_to_running_on_dispatch(conn, worker_id):
    sid = force_study(conn, status="approved")
    force_job(conn, sid, state="queued")
    sync_study_status(conn)
    assert study_row(conn, sid)["status"] == "approved"  # nothing dispatched yet
    force_job(conn, sid, state="running", worker=worker_id)
    sync_study_status(conn)
    assert study_row(conn, sid)["status"] == "running"
    assert events_for(conn, sid)[-1]["verb"] == "study.started"


def test_sync_study_running_to_review_when_all_terminal(conn):
    sid = force_study(conn, status="running")
    force_job(conn, sid, state="accepted")
    pending = force_job(conn, sid, state="escalated")
    sync_study_status(conn)
    assert study_row(conn, sid)["status"] == "running"  # escalated blocks review
    transition(conn, pending, "infeasible", "human:alex")
    sync_study_status(conn)
    assert study_row(conn, sid)["status"] == "review"
    assert events_for(conn, sid)[-1]["verb"] == "study.review_ready"
