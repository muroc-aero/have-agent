"""Exhaustive coverage of the §3.2 job state machine.

Every ordered (from, to) pair over all 15 states is exercised: the pairs in
JOB_TRANSITIONS must succeed (with the right actor and side effects, and an
event with the mapped verb); every other pair must raise IllegalTransition
and leave both the job row and the event log untouched.
"""

import pytest

from have_agent import transition
from have_agent.substrate import (
    _JOB_HUMAN_OR_MACHINE,
    _JOB_HUMAN_REQUIRED,
    JOB_STATES,
    JOB_TRANSITIONS,
    ActorNotAllowed,
    IllegalTransition,
    JobNotFound,
    StaleState,
    SubstrateError,
)
from tests.conftest import WORKER_ID, events_for, force_job, job_row

HUMAN = "human:alex"
SYSTEM = "system:scheduler"

ALL_PAIRS = sorted((a, b) for a in JOB_STATES for b in JOB_STATES if a != b)
LEGAL_PAIRS = sorted(JOB_TRANSITIONS)
ILLEGAL_PAIRS = sorted(set(ALL_PAIRS) - set(LEGAL_PAIRS))


def run_transition(conn, job_id, pair, actor=None, **kwargs):
    """Drive a legal transition with a permitted actor and required kwargs."""
    if actor is None:
        actor = HUMAN if pair in _JOB_HUMAN_REQUIRED else SYSTEM
    if pair == ("queued", "assigned"):
        kwargs.setdefault("worker_id", WORKER_ID)
        kwargs.setdefault("lease_expires_at", "9999-01-01T00:00:00+00:00")
    return transition(conn, job_id, pair[1], actor, **kwargs)


@pytest.mark.parametrize("pair", LEGAL_PAIRS, ids=lambda p: f"{p[0]}->{p[1]}")
def test_legal_transition(conn, study_id, worker_id, pair):
    frm, to = pair
    job = force_job(conn, study_id, state=frm, worker=worker_id)
    run_transition(conn, job, pair)

    row = job_row(conn, job)
    assert row["state"] == to
    events = events_for(conn, job)
    assert len(events) == 1
    assert events[0]["verb"] == JOB_TRANSITIONS[pair]
    assert events[0]["object_type"] == "job"
    import json

    payload = json.loads(events[0]["payload_json"])
    assert payload["from"] == frm
    assert payload["to"] == to


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=lambda p: f"{p[0]}->{p[1]}")
def test_illegal_transition(conn, study_id, worker_id, pair):
    frm, to = pair
    job = force_job(conn, study_id, state=frm, worker=worker_id)
    with pytest.raises(IllegalTransition):
        # actor checks come after legality checks, so any actor exposes them
        transition(conn, job, to, SYSTEM)
    assert job_row(conn, job)["state"] == frm
    assert events_for(conn, job) == []


def test_every_state_reachable_or_initial():
    reachable = {to for _, to in LEGAL_PAIRS} | {"proposed"}
    assert reachable == JOB_STATES


# --- actor rules -----------------------------------------------------------


@pytest.mark.parametrize("pair", sorted(_JOB_HUMAN_REQUIRED), ids=lambda p: f"{p[0]}->{p[1]}")
@pytest.mark.parametrize("actor", [SYSTEM, "agent:have", "worker:test-1"])
def test_human_required_rejects_machines(conn, study_id, worker_id, pair, actor):
    job = force_job(conn, study_id, state=pair[0], worker=worker_id)
    with pytest.raises(ActorNotAllowed):
        run_transition(conn, job, pair, actor=actor)
    assert job_row(conn, job)["state"] == pair[0]
    assert events_for(conn, job) == []


MACHINE_ONLY = sorted(set(LEGAL_PAIRS) - _JOB_HUMAN_REQUIRED - _JOB_HUMAN_OR_MACHINE)


@pytest.mark.parametrize("pair", MACHINE_ONLY, ids=lambda p: f"{p[0]}->{p[1]}")
def test_machine_only_rejects_humans(conn, study_id, worker_id, pair):
    job = force_job(conn, study_id, state=pair[0], worker=worker_id)
    with pytest.raises(ActorNotAllowed):
        run_transition(conn, job, pair, actor=HUMAN)
    assert job_row(conn, job)["state"] == pair[0]


def test_auto_accept_allows_both_actor_classes(conn, study_id):
    for actor in (HUMAN, SYSTEM):
        job = force_job(conn, study_id, state="succeeded")
        transition(conn, job, "accepted", actor)
        assert job_row(conn, job)["state"] == "accepted"


def test_cancel_allows_both_actor_classes(conn, study_id):
    for actor in (HUMAN, SYSTEM):
        job = force_job(conn, study_id, state="queued")
        transition(conn, job, "cancelled", actor)
        assert job_row(conn, job)["state"] == "cancelled"


def test_approve_allows_both_actor_classes(conn, study_id):
    """Humans approve plans; agents auto-approve within-policy retries."""
    for actor in (HUMAN, "agent:have"):
        job = force_job(conn, study_id, state="proposed")
        transition(conn, job, "approved", actor)
        assert job_row(conn, job)["state"] == "approved"


def test_malformed_actor_rejected(conn, study_id):
    job = force_job(conn, study_id, state="approved")
    for actor in ("alex", "pilot:alex", "human:", ""):
        with pytest.raises(SubstrateError):
            transition(conn, job, "queued", actor)


# --- side effects ----------------------------------------------------------


def test_claim_sets_worker_and_lease(conn, study_id, worker_id):
    job = force_job(conn, study_id, state="queued")
    lease = "2026-07-02T12:00:00+00:00"
    transition(conn, job, "assigned", SYSTEM, worker_id=worker_id, lease_expires_at=lease)
    row = job_row(conn, job)
    assert row["assigned_worker"] == worker_id
    assert row["lease_expires_at"] == lease


def test_claim_without_worker_or_lease_fails(conn, study_id, worker_id):
    job = force_job(conn, study_id, state="queued")
    with pytest.raises(SubstrateError):
        transition(conn, job, "assigned", SYSTEM, worker_id=worker_id)
    with pytest.raises(SubstrateError):
        transition(conn, job, "assigned", SYSTEM, lease_expires_at="2026-07-02T12:00:00+00:00")
    assert job_row(conn, job)["state"] == "queued"


@pytest.mark.parametrize("frm", ["assigned", "running"])
def test_lease_expiry_requeues_and_increments_attempt(conn, study_id, worker_id, frm):
    job = force_job(conn, study_id, state=frm, worker=worker_id, attempt=1)
    transition(conn, job, "queued", "system:reaper")
    row = job_row(conn, job)
    assert row["state"] == "queued"
    assert row["attempt"] == 2
    assert row["assigned_worker"] is None
    assert row["lease_expires_at"] is None
    assert events_for(conn, job)[0]["verb"] == "job.lease_expired"


@pytest.mark.parametrize(
    ("frm", "to"), [("running", "succeeded"), ("running", "failed"), ("assigned", "cancelled")]
)
def test_lease_cleared_on_leaving_leased_states(conn, study_id, worker_id, frm, to):
    job = force_job(conn, study_id, state=frm, worker=worker_id)
    transition(conn, job, to, SYSTEM)
    row = job_row(conn, job)
    assert row["lease_expires_at"] is None
    assert row["assigned_worker"] == worker_id  # kept for lineage


def test_start_can_extend_lease(conn, study_id, worker_id):
    job = force_job(conn, study_id, state="assigned", worker=worker_id)
    transition(conn, job, "running", "worker:test-1", lease_expires_at="2027-01-01T00:00:00+00:00")
    assert job_row(conn, job)["lease_expires_at"] == "2027-01-01T00:00:00+00:00"


def test_success_records_run_ref_and_artifacts(conn, study_id, worker_id):
    job = force_job(conn, study_id, state="running", worker=worker_id)
    transition(
        conn, job, "succeeded", "worker:test-1",
        run_ref="run_123", artifact_refs=["a1", "a2"],
    )
    row = job_row(conn, job)
    assert row["run_ref"] == "run_123"
    assert row["artifact_refs_json"] == '["a1", "a2"]'


# --- guards ----------------------------------------------------------------


def test_unknown_job(conn):
    with pytest.raises(JobNotFound):
        transition(conn, "no-such-job", "approved", HUMAN)


def test_unknown_state_name(conn, study_id):
    job = force_job(conn, study_id, state="proposed")
    with pytest.raises(IllegalTransition):
        transition(conn, job, "warp", HUMAN)


def test_expected_state_mismatch(conn, study_id):
    job = force_job(conn, study_id, state="queued")
    with pytest.raises(StaleState):
        transition(conn, job, "queued", SYSTEM, expected_state="approved")
    assert job_row(conn, job)["state"] == "queued"


def test_self_transition_illegal(conn, study_id):
    job = force_job(conn, study_id, state="queued")
    with pytest.raises(IllegalTransition):
        transition(conn, job, "queued", SYSTEM)


def test_atomicity_event_failure_rolls_back_job_update(conn, study_id):
    """If the event write fails, the job update must not survive."""
    job = force_job(conn, study_id, state="proposed")
    with pytest.raises(TypeError):
        # payload merges into the event's payload_json; an unserializable
        # value blows up json.dumps after the job UPDATE has run
        transition(conn, job, "approved", HUMAN, payload={"bad": object()})
    assert job_row(conn, job)["state"] == "proposed"
    assert events_for(conn, job) == []


def test_transition_records_prov_ref_and_payload(conn, study_id):
    job = force_job(conn, study_id, state="proposed")
    transition(conn, job, "approved", HUMAN, payload={"note": "lgtm"}, prov_ref="prov:act:1")
    ev = events_for(conn, job)[0]
    assert ev["prov_ref"] == "prov:act:1"
    assert '"note": "lgtm"' in ev["payload_json"]


def test_full_happy_path_walk(conn, study_id, worker_id):
    """proposed -> ... -> accepted via review, checking the event trail."""
    job = force_job(conn, study_id, state="proposed")
    transition(conn, job, "approved", HUMAN)
    transition(conn, job, "queued", SYSTEM)
    transition(conn, job, "assigned", SYSTEM, worker_id=worker_id,
               lease_expires_at="9999-01-01T00:00:00+00:00")
    transition(conn, job, "running", "worker:test-1")
    transition(conn, job, "succeeded", "worker:test-1", run_ref="run_1")
    transition(conn, job, "review", SYSTEM)
    transition(conn, job, "accepted", HUMAN)
    assert [e["verb"] for e in events_for(conn, job)] == [
        "job.approved", "job.enqueued", "job.claimed", "job.started",
        "job.succeeded", "job.review_requested", "job.accepted",
    ]


def test_failure_triage_walk(conn, study_id, worker_id):
    job = force_job(conn, study_id, state="running", worker=worker_id)
    transition(conn, job, "failed", "worker:test-1")
    transition(conn, job, "triage", "agent:have")
    transition(conn, job, "retry_spawned", "agent:have")
    assert [e["verb"] for e in events_for(conn, job)] == [
        "job.failed", "job.triage_started", "job.retried",
    ]
