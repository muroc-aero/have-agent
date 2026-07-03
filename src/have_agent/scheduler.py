"""Scheduler: runnable-job query, SchedulerPolicy, claim protocol, leases, reaper.

The claim is CAS-based rather than the spec's single UPDATE...RETURNING so
that every state mutation still goes through transition() — same atomicity,
one write surface (DECISIONS.md #9).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from have_agent.db import utcnow
from have_agent.substrate import IllegalTransition, StaleState, transition

MIN_LEASE_S = 600  # spec §2.3: lease = 2x estimated runtime, min 10 min
LEASE_FACTOR = 2

# Terminal for REPORT dep purposes (§2.2): the report covers failures too.
# retry_spawned counts — the lineage continues in the retry job, which
# dependents are repointed to (DECISIONS.md #12).
REPORT_DEP_TERMINAL = frozenset(
    {"accepted", "rejected", "infeasible", "cancelled", "retry_spawned"}
)


@dataclass(frozen=True)
class JobInfo:
    id: str
    study_id: str
    type: str
    priority: int
    created_at: str
    resource: dict[str, Any]
    attempt: int
    max_attempts: int


@dataclass(frozen=True)
class WorkerSlot:
    worker_id: str
    capabilities: dict[str, Any]
    free_slots: int


@dataclass(frozen=True)
class Assignment:
    job_id: str
    worker_id: str


class SchedulerPolicy(Protocol):
    def select(
        self, runnable: list[JobInfo], workers: list[WorkerSlot]
    ) -> list[Assignment]: ...


def fits(job: JobInfo, slot: WorkerSlot) -> bool:
    caps = slot.capabilities
    if not set(job.resource.get("requires", ())) <= set(caps.get("solvers", ())):
        return False
    mem = job.resource.get("mem_mb")
    return not (mem is not None and caps.get("mem_mb") is not None and mem > caps["mem_mb"])


class GreedyPriority:
    """§6 v0 policy: priority, then FIFO, first-fit on resources."""

    def select(
        self, runnable: list[JobInfo], workers: list[WorkerSlot]
    ) -> list[Assignment]:
        assignments: list[Assignment] = []
        taken: set[str] = set()
        for slot in workers:
            for _ in range(slot.free_slots):
                job = next(
                    (j for j in runnable if j.id not in taken and fits(j, slot)), None
                )
                if job is None:
                    break
                assignments.append(Assignment(job.id, slot.worker_id))
                taken.add(job.id)
        return assignments


def _dep_satisfied(
    dep_state: str, dep_attempt: int, dep_max: int, job_type: str, policy: dict
) -> bool:
    """§2.2 dep semantics v0."""
    if job_type == "REPORT":
        return dep_state in REPORT_DEP_TERMINAL or (
            dep_state == "failed" and dep_attempt >= dep_max  # exhausted failed
        )
    if dep_state == "accepted":
        return True
    # succeeded counts when the study policy auto-accepts (DECISIONS.md #13):
    # this is what lets a CHECK run against its succeeded-but-not-yet-accepted
    # ANALYSIS — acceptance comes *after* the check verdict.
    return dep_state == "succeeded" and bool(policy.get("auto_accept"))


def runnable_jobs(conn: sqlite3.Connection) -> list[JobInfo]:
    """Queued jobs whose deps are satisfied, in (priority, created_at) order."""
    rows = conn.execute(
        "SELECT id, study_id, type, priority, created_at, resource_json,"
        " attempt, max_attempts FROM job WHERE state = 'queued'"
        " ORDER BY priority, created_at"
    ).fetchall()
    if not rows:
        return []
    policies = {
        r["id"]: json.loads(r["policy_json"])
        for r in conn.execute("SELECT id, policy_json FROM study")
    }
    deps: dict[str, list[sqlite3.Row]] = {}
    for d in conn.execute(
        "SELECT d.job_id AS jid, u.state AS ustate, u.attempt AS uattempt,"
        " u.max_attempts AS umax FROM job_dep d JOIN job u ON u.id = d.depends_on"
    ):
        deps.setdefault(d["jid"], []).append(d)
    runnable = []
    for r in rows:
        policy = policies.get(r["study_id"], {})
        if all(
            _dep_satisfied(d["ustate"], d["uattempt"], d["umax"], r["type"], policy)
            for d in deps.get(r["id"], ())
        ):
            runnable.append(
                JobInfo(
                    id=r["id"], study_id=r["study_id"], type=r["type"],
                    priority=r["priority"], created_at=r["created_at"],
                    resource=json.loads(r["resource_json"]),
                    attempt=r["attempt"], max_attempts=r["max_attempts"],
                )
            )
    return runnable


def lease_duration_s(resource: dict[str, Any]) -> int:
    return max(MIN_LEASE_S, LEASE_FACTOR * int(resource.get("est_runtime_s", 0)))


def lease_expiry(resource: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return (now + timedelta(seconds=lease_duration_s(resource))).isoformat(
        timespec="microseconds"
    )


def claim_next(
    conn: sqlite3.Connection,
    worker_id: str,
    policy: SchedulerPolicy | None = None,
) -> sqlite3.Row | None:
    """Atomically claim the next runnable job for worker_id, or None.

    Optimistic CAS loop: pick per policy, claim via transition() guarded on
    state='queued'; a lost race (StaleState) drops the job and re-selects.
    """
    policy = policy or GreedyPriority()
    w = conn.execute("SELECT * FROM worker WHERE id = ?", (worker_id,)).fetchone()
    if w is None or w["status"] != "online":
        return None
    busy = conn.execute(
        "SELECT COUNT(*) FROM job WHERE assigned_worker = ?"
        " AND state IN ('assigned', 'running')",
        (worker_id,),
    ).fetchone()[0]
    if w["capacity"] - busy <= 0:
        return None
    slot = WorkerSlot(worker_id, json.loads(w["capabilities_json"]), 1)
    runnable = runnable_jobs(conn)
    while runnable:
        picks = policy.select(runnable, [slot])
        if not picks:
            return None
        job = next(j for j in runnable if j.id == picks[0].job_id)
        try:
            transition(
                conn, job.id, "assigned", worker_id,
                expected_state="queued",
                worker_id=worker_id,
                lease_expires_at=lease_expiry(job.resource),
            )
            return conn.execute("SELECT * FROM job WHERE id = ?", (job.id,)).fetchone()
        except StaleState:
            runnable = [j for j in runnable if j.id != job.id]
    return None


def heartbeat(conn: sqlite3.Connection, worker_id: str) -> int:
    """Refresh worker.last_heartbeat and extend leases on this worker's
    active jobs (each by its own lease duration from now). Returns the
    number of leases extended. Lease is not state, so this writes directly
    (DECISIONS.md #9)."""
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE worker SET last_heartbeat = ? WHERE id = ?", (now, worker_id)
        )
        extended = 0
        for row in conn.execute(
            "SELECT id, resource_json FROM job WHERE assigned_worker = ?"
            " AND state IN ('assigned', 'running')",
            (worker_id,),
        ).fetchall():
            conn.execute(
                "UPDATE job SET lease_expires_at = ? WHERE id = ?",
                (lease_expiry(json.loads(row["resource_json"])), row["id"]),
            )
            extended += 1
        conn.execute("COMMIT")
        return extended
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def reap_expired(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Requeue expired-lease jobs (attempt+1); fail them once attempts are
    exhausted (DECISIONS.md #10). Returns [(job_id, action)]."""
    now = utcnow()
    rows = conn.execute(
        "SELECT id, state, attempt, max_attempts FROM job"
        " WHERE state IN ('assigned', 'running') AND lease_expires_at < ?",
        (now,),
    ).fetchall()
    actions = []
    for r in rows:
        exhausted = r["attempt"] >= r["max_attempts"]
        to_state = "failed" if exhausted else "queued"
        payload = {"reason": "lease_expired", "exhausted": exhausted}
        try:
            transition(
                conn, r["id"], to_state, "system:reaper", payload,
                expected_state=r["state"],
            )
        except (StaleState, IllegalTransition):
            continue  # worker finished or another reaper won the race
        actions.append((r["id"], to_state))
    return actions
