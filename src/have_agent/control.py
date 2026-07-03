"""Deterministic control loop: the machine-territory transitions.

Each function is idempotent and race-safe (CAS via transition(); losers of a
race skip). Any worker or process may run control_tick() concurrently.
"""

import json
import sqlite3

from have_agent.scheduler import reap_expired
from have_agent.substrate import (
    TERMINAL_JOB_STATES,
    IllegalTransition,
    StaleState,
    create_job,
    study_transition,
    transition,
)

AGENT = "agent:have"
SCHEDULER = "system:scheduler"

_TERMINAL_SQL = ", ".join(f"'{s}'" for s in sorted(TERMINAL_JOB_STATES))


def _try(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except (StaleState, IllegalTransition):
        return False  # lost a race with another control loop; next tick catches up


def enqueue_approved(conn: sqlite3.Connection) -> int:
    n = 0
    for r in conn.execute("SELECT id FROM job WHERE state = 'approved'").fetchall():
        n += _try(transition, conn, r["id"], "queued", SCHEDULER, expected_state="approved")
    return n


def spawn_triage(conn: sqlite3.Connection) -> list[str]:
    """failed --spawn TRIAGE--> triage (§3.2). Parent flips first (the CAS is
    the spawn lock); creating the TRIAGE child is retried by the repair arm
    if a crash lands between the two."""
    spawned = []
    for r in conn.execute("SELECT * FROM job WHERE state = 'failed'").fetchall():
        if not _try(transition, conn, r["id"], "triage", AGENT, expected_state="failed"):
            continue
        spawned.append(_create_triage_job(conn, r))
    # repair arm: triage-state jobs missing their TRIAGE child
    for r in conn.execute(
        "SELECT * FROM job WHERE state = 'triage' AND id NOT IN"
        " (SELECT parent_job_id FROM job WHERE type = 'TRIAGE'"
        "  AND parent_job_id IS NOT NULL)"
    ).fetchall():
        spawned.append(_create_triage_job(conn, r))
    return spawned


def _create_triage_job(conn: sqlite3.Connection, failed_job: sqlite3.Row) -> str:
    triage_id = create_job(
        conn,
        failed_job["study_id"],
        "TRIAGE",
        payload={
            "failed_job_id": failed_job["id"],
            "context": {"log_tail": True, "neighbor_cases": True},
        },
        actor=AGENT,
        priority=max(0, failed_job["priority"] - 10),  # triage jumps the queue
        resource={"est_runtime_s": 30},
        parent_job_id=failed_job["id"],
    )
    transition(conn, triage_id, "approved", AGENT)  # machine job, auto-approved
    return triage_id


def apply_verdict_gates(conn: sqlite3.Connection) -> int:
    """After a CHECK succeeds with a verdict: gate its upstream ANALYSIS
    (auto-accept on the policy level, review on gate_on), then accept the
    CHECK itself. Upstream first, so a crash in between is healed by the
    next tick re-reading the still-succeeded CHECK."""
    n = 0
    rows = conn.execute(
        "SELECT j.id, j.study_id, j.verdict_id, v.level, s.policy_json FROM job j"
        " JOIN verdict v ON v.id = j.verdict_id"
        " JOIN study s ON s.id = j.study_id"
        " WHERE j.type = 'CHECK' AND j.state = 'succeeded'"
        " AND j.verdict_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        policy = json.loads(r["policy_json"])
        auto_level = policy.get("auto_accept", {}).get("verdict_level", "pass")
        gate_on = policy.get("gate_on", ["warn", "fail"])
        upstream = conn.execute(
            "SELECT u.id, u.state FROM job_dep d JOIN job u ON u.id = d.depends_on"
            " WHERE d.job_id = ?",
            (r["id"],),
        ).fetchone()
        if upstream is not None and upstream["state"] == "succeeded":
            to = "accepted" if r["level"] == auto_level and r["level"] not in gate_on else "review"
            n += _try(
                transition, conn, upstream["id"], to, AGENT,
                {"verdict_id": r["verdict_id"], "level": r["level"]},
                expected_state="succeeded",
            )
        n += _try(
            transition, conn, r["id"], "accepted", AGENT,
            {"level": r["level"]}, expected_state="succeeded",
        )
    return n


def auto_accept_support_jobs(conn: sqlite3.Connection) -> int:
    """TRIAGE and REPORT jobs have no acceptance gate of their own."""
    n = 0
    for r in conn.execute(
        "SELECT id FROM job WHERE type IN ('TRIAGE', 'REPORT') AND state = 'succeeded'"
    ).fetchall():
        n += _try(transition, conn, r["id"], "accepted", AGENT, expected_state="succeeded")
    return n


def cancel_dead_branches(conn: sqlite3.Connection) -> int:
    """Cancel pre-queue jobs whose deps can never be satisfied (upstream
    rejected/infeasible/cancelled). REPORT is exempt — its deps treat those
    as satisfied terminals."""
    n = 0
    for r in conn.execute(
        "SELECT DISTINCT j.id, j.state FROM job j"
        " JOIN job_dep d ON d.job_id = j.id"
        " JOIN job u ON u.id = d.depends_on"
        " WHERE j.state IN ('proposed', 'approved', 'queued') AND j.type != 'REPORT'"
        " AND u.state IN ('rejected', 'infeasible', 'cancelled')"
    ).fetchall():
        n += _try(
            transition, conn, r["id"], "cancelled", SCHEDULER,
            {"reason": "upstream_dead"}, expected_state=r["state"],
        )
    return n


def sync_study_status(conn: sqlite3.Connection) -> None:
    # approved -> running on first job dispatch (§3.1)
    for s in conn.execute("SELECT id FROM study WHERE status = 'approved'").fetchall():
        dispatched = conn.execute(
            "SELECT 1 FROM job WHERE study_id = ? AND state NOT IN"
            " ('proposed', 'approved', 'queued', 'cancelled') LIMIT 1",
            (s["id"],),
        ).fetchone()
        if dispatched:
            _try(study_transition, conn, s["id"], "running", SCHEDULER,
                 expected_status="approved")
    # running -> review when all jobs terminal (§3.1)
    for s in conn.execute("SELECT id FROM study WHERE status = 'running'").fetchall():
        pending = conn.execute(
            f"SELECT 1 FROM job WHERE study_id = ? AND state NOT IN ({_TERMINAL_SQL})"
            " LIMIT 1",
            (s["id"],),
        ).fetchone()
        if not pending:
            _try(study_transition, conn, s["id"], "review", SCHEDULER,
                 expected_status="running")


def control_tick(conn: sqlite3.Connection) -> dict[str, int]:
    stats = {
        "reaped": len(reap_expired(conn)),
        "enqueued": enqueue_approved(conn),
        "triage_spawned": len(spawn_triage(conn)),
        "gated": apply_verdict_gates(conn),
        "auto_accepted": auto_accept_support_jobs(conn),
        "cancelled": cancel_dead_branches(conn),
    }
    sync_study_status(conn)
    return stats
