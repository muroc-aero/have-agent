"""The substrate write surface.

Every job.state mutation goes through transition(); every study.status
mutation goes through study_transition(). Both write the row update and
the corresponding event in one transaction. Nothing else may mutate
state, and the event table is append-only.
"""

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from have_agent.db import utcnow
from have_agent.ids import new_ulid


class SubstrateError(Exception):
    """Base for substrate violations."""


class JobNotFound(SubstrateError):
    pass


class StudyNotFound(SubstrateError):
    pass


class IllegalTransition(SubstrateError):
    pass


class ActorNotAllowed(SubstrateError):
    pass


class StaleState(SubstrateError):
    """Row was not in the expected state (lost a race or caller is out of date)."""


ACTOR_CLASSES = frozenset({"human", "agent", "worker", "system"})

JOB_TYPES = frozenset({"ANALYSIS", "CHECK", "TRIAGE", "REPORT", "WATCH"})

JOB_STATES = frozenset(
    {
        "proposed", "approved", "queued", "assigned", "running",
        "succeeded", "failed", "review", "accepted", "rejected",
        "triage", "retry_spawned", "infeasible", "escalated", "cancelled",
    }
)

# States with no further transitions for this job row. 'failed' is terminal
# only once attempts are exhausted (dynamic — scheduler's concern, §2.2);
# 'escalated' waits on a human, so neither appears here.
TERMINAL_JOB_STATES = frozenset(
    {"accepted", "rejected", "retry_spawned", "infeasible", "cancelled"}
)

_PRE_RUNNING = ("proposed", "approved", "queued", "assigned")

# (from, to) -> event verb. Spec §3.2/§3.3. 'job.triage_started' is an
# addition to the §3.3 vocabulary (failed->triage had no verb) — DECISIONS.md #1.
JOB_TRANSITIONS: dict[tuple[str, str], str] = {
    ("proposed", "approved"): "job.approved",
    ("approved", "queued"): "job.enqueued",
    ("queued", "assigned"): "job.claimed",
    ("assigned", "running"): "job.started",
    ("running", "succeeded"): "job.succeeded",
    ("running", "failed"): "job.failed",
    # reaper: lease expired with attempts exhausted, worker died between
    # claim and start — DECISIONS.md #10
    ("assigned", "failed"): "job.failed",
    ("succeeded", "accepted"): "job.accepted",
    ("succeeded", "review"): "job.review_requested",
    ("review", "accepted"): "job.accepted",
    ("review", "rejected"): "job.rejected",
    ("failed", "triage"): "job.triage_started",
    ("triage", "retry_spawned"): "job.retried",
    ("triage", "infeasible"): "job.infeasible",
    ("triage", "escalated"): "job.escalated",
    # reaper: expired lease back to queued (attempt+1)
    ("assigned", "queued"): "job.lease_expired",
    ("running", "queued"): "job.lease_expired",
    # escalated->* is human-touchable; v0 allows the resolutions that make
    # sense (re-run, kill, write off) — DECISIONS.md #2.
    ("escalated", "queued"): "job.enqueued",
    ("escalated", "cancelled"): "job.cancelled",
    ("escalated", "infeasible"): "job.infeasible",
    **{(s, "cancelled"): "job.cancelled" for s in _PRE_RUNNING},
}

_LEASE_EXPIRY = {("assigned", "queued"), ("running", "queued")}

# Transitions only a human may perform (spec §3.2 "human-touchable", minus
# cancellation, which study-abort cascades also need, and minus
# proposed->approved, which policy-bounded auto-retries also need —
# DECISIONS.md #3).
_JOB_HUMAN_REQUIRED = {
    ("review", "accepted"),
    ("review", "rejected"),
    ("escalated", "queued"),
    ("escalated", "cancelled"),
    ("escalated", "infeasible"),
}

# Transitions either side may perform: approval (humans approve plans,
# agents auto-approve within-policy retries — DECISIONS.md #3), auto/human
# accept, and pre-running cancels (study-abort cascades cancel with a
# system actor). Cancelling an escalated job stays human-only — escalated
# is a human-decision state.
_JOB_HUMAN_OR_MACHINE = {("proposed", "approved"), ("succeeded", "accepted")} | {
    (frm, "cancelled") for frm in _PRE_RUNNING
}

STUDY_STATUSES = frozenset(
    {"draft", "proposed", "approved", "running", "review", "closed", "aborted"}
)

_STUDY_NON_TERMINAL = ("draft", "proposed", "approved", "running", "review")

# 'study.started' and 'study.review_ready' are additions to the §3.3
# vocabulary (approved->running and running->review had no verb) — DECISIONS.md #1.
STUDY_TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "proposed"): "study.plan_proposed",
    ("proposed", "approved"): "study.approved",
    ("approved", "running"): "study.started",
    ("running", "review"): "study.review_ready",
    ("review", "closed"): "study.closed",
    **{(s, "aborted"): "study.aborted" for s in _STUDY_NON_TERMINAL},
}

_STUDY_HUMAN_REQUIRED = {("proposed", "approved"), ("review", "closed")} | {
    (s, "aborted") for s in _STUDY_NON_TERMINAL
}

EVENT_VERBS = frozenset(
    {
        "study.submitted", "study.plan_proposed", "study.approved",
        "study.started", "study.review_ready",  # added, DECISIONS.md #1
        "study.aborted", "study.closed",
        "job.proposed", "job.approved", "job.enqueued", "job.claimed",
        "job.started", "job.succeeded", "job.failed", "job.lease_expired",
        "job.retried", "job.cancelled", "job.review_requested", "job.accepted",
        "job.rejected", "job.escalated", "job.infeasible",
        "job.triage_started",  # added, DECISIONS.md #1
        "verdict.recorded",
        "worker.registered", "worker.heartbeat", "worker.draining", "worker.offline",
        "decision.logged",
        "report.published",
    }
)

EVENT_OBJECT_TYPES = frozenset({"study", "job", "worker", "verdict"})


def _actor_class(actor: str) -> str:
    cls, sep, name = actor.partition(":")
    if not sep or not name or cls not in ACTOR_CLASSES:
        raise SubstrateError(
            f"malformed actor {actor!r}; expected '<class>:<name>'"
            f" with class in {sorted(ACTOR_CLASSES)}"
        )
    return cls


def _check_job_actor(actor: str, pair: tuple[str, str]) -> None:
    is_human = _actor_class(actor) == "human"
    if pair in _JOB_HUMAN_OR_MACHINE:
        return
    if pair in _JOB_HUMAN_REQUIRED:
        if not is_human:
            raise ActorNotAllowed(f"{pair[0]} -> {pair[1]} requires a human actor, got {actor!r}")
    elif is_human:
        raise ActorNotAllowed(f"{pair[0]} -> {pair[1]} is machine territory, got {actor!r}")


def _check_study_actor(actor: str, pair: tuple[str, str]) -> None:
    is_human = _actor_class(actor) == "human"
    if pair in _STUDY_HUMAN_REQUIRED:
        if not is_human:
            raise ActorNotAllowed(f"{pair[0]} -> {pair[1]} requires a human actor, got {actor!r}")
    elif is_human:
        raise ActorNotAllowed(f"{pair[0]} -> {pair[1]} is machine territory, got {actor!r}")


def _insert_event(
    conn: sqlite3.Connection,
    ts: str,
    actor: str,
    verb: str,
    object_type: str,
    object_id: str,
    payload: dict[str, Any] | None,
    prov_ref: str | None,
) -> str:
    if verb not in EVENT_VERBS:
        raise SubstrateError(f"unknown event verb {verb!r}")
    if object_type not in EVENT_OBJECT_TYPES:
        raise SubstrateError(f"unknown event object_type {object_type!r}")
    event_id = new_ulid()
    conn.execute(
        "INSERT INTO event (id, ts, actor, verb, object_type, object_id, payload_json, prov_ref)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, ts, actor, verb, object_type, object_id, json.dumps(payload or {}), prov_ref),
    )
    return event_id


def emit_event(
    conn: sqlite3.Connection,
    actor: str,
    verb: str,
    object_type: str,
    object_id: str,
    payload: dict[str, Any] | None = None,
    prov_ref: str | None = None,
) -> str:
    """Append a standalone event (heartbeats, decisions, verdicts, reports)."""
    _actor_class(actor)
    conn.execute("BEGIN IMMEDIATE")
    try:
        event_id = _insert_event(
            conn, utcnow(), actor, verb, object_type, object_id, payload, prov_ref
        )
        conn.execute("COMMIT")
        return event_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def transition(
    conn: sqlite3.Connection,
    job_id: str,
    to_state: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    *,
    expected_state: str | None = None,
    worker_id: str | None = None,
    lease_expires_at: str | None = None,
    run_ref: str | None = None,
    verdict_id: str | None = None,
    artifact_refs: list[str] | None = None,
    prov_ref: str | None = None,
) -> str:
    """Move a job to to_state, writing job + event atomically. Returns event id.

    The ONLY legal writer of job.state. Raises IllegalTransition for moves
    outside the §3.2 state machine, ActorNotAllowed for actor-class
    violations, StaleState when expected_state (or a concurrent writer)
    disagrees with the current row.
    """
    if to_state not in JOB_STATES:
        raise IllegalTransition(f"unknown job state {to_state!r}")
    _actor_class(actor)
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        current = row["state"]
        if expected_state is not None and current != expected_state:
            raise StaleState(f"job {job_id} is {current!r}, expected {expected_state!r}")
        pair = (current, to_state)
        verb = JOB_TRANSITIONS.get(pair)
        if verb is None:
            raise IllegalTransition(f"job {job_id}: {current} -> {to_state}")
        _check_job_actor(actor, pair)

        sets: dict[str, Any] = {"state": to_state, "state_updated_at": now}
        if pair == ("queued", "assigned"):
            if not worker_id or not lease_expires_at:
                raise SubstrateError("claiming a job requires worker_id and lease_expires_at")
            sets["assigned_worker"] = worker_id
            sets["lease_expires_at"] = lease_expires_at
        elif pair in _LEASE_EXPIRY:
            sets["assigned_worker"] = None
            sets["lease_expires_at"] = None
            sets["attempt"] = row["attempt"] + 1
        elif to_state == "running":
            if lease_expires_at is not None:
                sets["lease_expires_at"] = lease_expires_at
        elif current in ("assigned", "running"):
            # leaving the leased states: lease must be NULL (§2.2);
            # assigned_worker is kept for lineage
            sets["lease_expires_at"] = None
        if run_ref is not None:
            sets["run_ref"] = run_ref
        if verdict_id is not None:
            sets["verdict_id"] = verdict_id
        if artifact_refs is not None:
            sets["artifact_refs_json"] = json.dumps(artifact_refs)

        assignments = ", ".join(f"{col} = ?" for col in sets)
        updated = conn.execute(
            f"UPDATE job SET {assignments} WHERE id = ? AND state = ?",  # noqa: S608
            (*sets.values(), job_id, current),
        )
        if updated.rowcount != 1:
            raise StaleState(f"job {job_id} changed state concurrently")
        event_payload = {"from": current, "to": to_state, **(payload or {})}
        event_id = _insert_event(conn, now, actor, verb, "job", job_id, event_payload, prov_ref)
        conn.execute("COMMIT")
        return event_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def study_transition(
    conn: sqlite3.Connection,
    study_id: str,
    to_status: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    *,
    expected_status: str | None = None,
    plan_proposal: dict[str, Any] | None = None,
    conclusion_ref: str | None = None,
    prov_ref: str | None = None,
) -> str:
    """Move a study to to_status, writing study + event atomically. Returns event id."""
    if to_status not in STUDY_STATUSES:
        raise IllegalTransition(f"unknown study status {to_status!r}")
    _actor_class(actor)
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM study WHERE id = ?", (study_id,)).fetchone()
        if row is None:
            raise StudyNotFound(study_id)
        current = row["status"]
        if expected_status is not None and current != expected_status:
            raise StaleState(f"study {study_id} is {current!r}, expected {expected_status!r}")
        pair = (current, to_status)
        verb = STUDY_TRANSITIONS.get(pair)
        if verb is None:
            raise IllegalTransition(f"study {study_id}: {current} -> {to_status}")
        _check_study_actor(actor, pair)

        sets: dict[str, Any] = {"status": to_status, "updated_at": now}
        if to_status in ("closed", "aborted"):
            sets["closed_at"] = now
        if plan_proposal is not None:
            sets["plan_proposal_json"] = json.dumps(plan_proposal)
        if conclusion_ref is not None:
            sets["conclusion_ref"] = conclusion_ref

        assignments = ", ".join(f"{col} = ?" for col in sets)
        updated = conn.execute(
            f"UPDATE study SET {assignments} WHERE id = ? AND status = ?",  # noqa: S608
            (*sets.values(), study_id, current),
        )
        if updated.rowcount != 1:
            raise StaleState(f"study {study_id} changed status concurrently")
        event_payload = {"from": current, "to": to_status, **(payload or {})}
        event_id = _insert_event(conn, now, actor, verb, "study", study_id, event_payload, prov_ref)
        conn.execute("COMMIT")
        return event_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_study(
    conn: sqlite3.Connection,
    title: str,
    intent_yaml: str,
    owner: str,
    policy: dict[str, Any],
    *,
    actor: str | None = None,
) -> str:
    """Insert a study in 'draft' + a study.submitted event. Returns study id."""
    _actor_class(owner)
    actor = actor or owner
    study_id = new_ulid()
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO study (id, title, intent_yaml, status, owner, policy_json,"
            " created_at, updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)",
            (study_id, title, intent_yaml, owner, json.dumps(policy), now, now),
        )
        _insert_event(
            conn, now, actor, "study.submitted", "study", study_id, {"title": title}, None
        )
        conn.execute("COMMIT")
        return study_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_job(
    conn: sqlite3.Connection,
    study_id: str,
    job_type: str,
    payload: dict[str, Any],
    actor: str,
    *,
    priority: int = 50,
    resource: dict[str, Any] | None = None,
    max_attempts: int = 3,
    attempt: int = 1,
    parent_job_id: str | None = None,
    deps: Iterable[str] = (),
    prov_ref: str | None = None,
) -> str:
    """Insert a job in 'proposed' (+ deps) + a job.proposed event. Returns job id."""
    if job_type not in JOB_TYPES:
        raise SubstrateError(f"unknown job type {job_type!r}")
    _actor_class(actor)
    job_id = new_ulid()
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO job (id, study_id, type, state, priority, resource_json, payload_json,"
            " attempt, max_attempts, parent_job_id, created_at, state_updated_at)"
            " VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id, study_id, job_type, priority,
                json.dumps(resource or {}), json.dumps(payload),
                attempt, max_attempts, parent_job_id, now, now,
            ),
        )
        for dep in deps:
            conn.execute(
                "INSERT INTO job_dep (job_id, depends_on) VALUES (?, ?)", (job_id, dep)
            )
        _insert_event(
            conn, now, actor, "job.proposed", "job", job_id,
            {"type": job_type, "study_id": study_id}, prov_ref,
        )
        conn.execute("COMMIT")
        return job_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def record_verdict(
    conn: sqlite3.Connection,
    job_id: str,
    level: str,
    checks: list[dict[str, Any]],
    actor: str,
    *,
    run_ref: str | None = None,
    summary: str | None = None,
    prov_ref: str | None = None,
) -> str:
    """Insert a verdict + a verdict.recorded event atomically. Returns verdict id."""
    _actor_class(actor)
    verdict_id = new_ulid()
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO verdict (id, job_id, run_ref, level, checks_json, summary, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (verdict_id, job_id, run_ref, level, json.dumps(checks), summary, now),
        )
        _insert_event(
            conn, now, actor, "verdict.recorded", "verdict", verdict_id,
            {"job_id": job_id, "level": level}, prov_ref,
        )
        conn.execute("COMMIT")
        return verdict_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def register_worker(
    conn: sqlite3.Connection,
    worker_id: str,
    capabilities: dict[str, Any],
    *,
    capacity: int = 1,
    meta: dict[str, Any] | None = None,
) -> None:
    """Insert or refresh a worker row + a worker.registered event."""
    if _actor_class(worker_id) != "worker":
        raise SubstrateError(f"worker id must look like 'worker:<name>', got {worker_id!r}")
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO worker"
            " (id, capabilities_json, capacity, status, last_heartbeat, meta_json)"
            " VALUES (?, ?, ?, 'online', ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET capabilities_json = excluded.capabilities_json,"
            " capacity = excluded.capacity, status = 'online',"
            " last_heartbeat = excluded.last_heartbeat, meta_json = excluded.meta_json",
            (worker_id, json.dumps(capabilities), capacity, now, json.dumps(meta or {})),
        )
        _insert_event(conn, now, worker_id, "worker.registered", "worker", worker_id, None, None)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
