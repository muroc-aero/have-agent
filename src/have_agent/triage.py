"""TRIAGE execution: deterministic v0, LLM hook stubbed.

Outcomes per §3.2/§4: retry_spawned (new job, parent_job_id set, attempt+1,
auto-approved within policy.auto_retry_max), infeasible (permanent failures),
escalated (attempts exhausted — a human decides). The recommendation is
logged as a decision.logged event.
"""

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from have_agent.substrate import create_job, emit_event, transition

AGENT = "agent:have"

# Optional hook: (failed_job_row_dict, context) -> {"outcome": ..., "reason": ...}
# overriding the deterministic recommendation. Stubbed for v0.
llm_triage_hook: Callable[[dict, dict], dict] | None = None


def _last_failure_payload(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_json FROM event WHERE object_id = ? AND verb = 'job.failed'"
        " ORDER BY ts DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else {}


def _numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nearest_converged_run(
    conn: sqlite3.Connection, study_id: str, overrides: dict[str, Any]
) -> tuple[str | None, str | None]:
    """retry_strategy=warm_start_nearest_converged: the converged sibling
    nearest to `overrides` in sweep coordinates. Numeric axes are normalized
    by their span across the candidates (so e.g. Wh/kg and nmi weigh
    equally); a key that is non-numeric or missing on either side costs 1
    when the values differ. Ties break on case_id, so the pick is
    deterministic and replayable. Returns (run_ref, case_id) or (None, None);
    the-hangar cold-starts on a miss either way."""
    rows = conn.execute(
        "SELECT run_ref, payload_json FROM job WHERE study_id = ? AND type = 'ANALYSIS'"
        " AND state IN ('succeeded', 'accepted') AND run_ref IS NOT NULL",
        (study_id,),
    ).fetchall()
    candidates = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        candidates.append(
            (row["run_ref"], payload.get("case_id", "?"), payload.get("overrides") or {})
        )
    if not candidates:
        return None, None

    target = overrides or {}
    keys = sorted({k for _, _, sib in candidates for k in sib} | set(target))
    spans: dict[str, float] = {}
    for k in keys:
        vals = [sib[k] for _, _, sib in candidates if _numeric(sib.get(k))]
        if _numeric(target.get(k)):
            vals.append(target[k])
        spans[k] = float(max(vals) - min(vals)) if vals else 0.0

    def distance(sib: dict[str, Any]) -> float:
        d = 0.0
        for k in keys:
            a, b = target.get(k), sib.get(k)
            if _numeric(a) and _numeric(b):
                d += ((a - b) / spans[k]) ** 2 if spans[k] else 0.0
            elif a != b:
                d += 1.0
        return d

    run_ref, case_id, _ = min(candidates, key=lambda c: (distance(c[2]), c[1]))
    return run_ref, case_id


def run_triage(conn: sqlite3.Connection, triage_job: sqlite3.Row) -> dict[str, Any]:
    """Execute a TRIAGE job. The failed parent must be in state 'triage'.
    Returns {"outcome": ..., "retry_job_id": ...?}."""
    payload = json.loads(triage_job["payload_json"])
    parent = conn.execute(
        "SELECT * FROM job WHERE id = ?", (payload["failed_job_id"],)
    ).fetchone()
    study = conn.execute(
        "SELECT policy_json FROM study WHERE id = ?", (parent["study_id"],)
    ).fetchone()
    policy = json.loads(study["policy_json"])
    failure = _last_failure_payload(conn, parent["id"])

    if failure.get("permanent"):
        decision = {"outcome": "infeasible", "reason": failure.get("error", "permanent failure")}
    elif parent["attempt"] >= parent["max_attempts"]:
        decision = {"outcome": "escalated", "reason": "attempts exhausted"}
    else:
        decision = {"outcome": "retry", "reason": failure.get("error", "transient failure")}
    if llm_triage_hook is not None:
        decision = llm_triage_hook(dict(parent), {"failure": failure, "policy": policy})

    result: dict[str, Any] = {"outcome": decision["outcome"]}
    if decision["outcome"] == "retry":
        retry_number = parent["attempt"]  # 1st retry follows attempt 1
        auto = retry_number <= int(policy.get("auto_retry_max", 0))
        parent_payload = json.loads(parent["payload_json"])
        if policy.get("retry_strategy") == "warm_start_nearest_converged":
            warm_ref, warm_case = _nearest_converged_run(
                conn, parent["study_id"], parent_payload.get("overrides") or {}
            )
            parent_payload["warm_start_run"] = warm_ref
            if warm_ref:
                result.update({"warm_start_run": warm_ref, "warm_start_case": warm_case})
        retry_id = create_job(
            conn, parent["study_id"], parent["type"],
            payload=parent_payload,
            actor=AGENT,
            priority=parent["priority"],
            resource=json.loads(parent["resource_json"]),
            max_attempts=parent["max_attempts"],
            attempt=parent["attempt"] + 1,
            parent_job_id=parent["id"],
        )
        # repoint dependents (CHECK etc.) at the retry — the lineage
        # continues there (DECISIONS.md #12)
        conn.execute(
            "UPDATE job_dep SET depends_on = ? WHERE depends_on = ?",
            (retry_id, parent["id"]),
        )
        if auto:
            transition(conn, retry_id, "approved", AGENT, {"auto_retry": retry_number})
        transition(
            conn, parent["id"], "retry_spawned", AGENT,
            {"retry_job_id": retry_id, "auto_approved": auto},
            expected_state="triage",
        )
        result.update({"retry_job_id": retry_id, "auto_approved": auto})
    else:
        transition(
            conn, parent["id"], decision["outcome"], AGENT,
            {"reason": decision["reason"]},
            expected_state="triage",
        )
    emit_event(
        conn, AGENT, "decision.logged", "job", parent["id"],
        {"decision": "triage_" + decision["outcome"], "reason": decision["reason"],
         "triage_job_id": triage_job["id"], **{k: v for k, v in result.items() if k != "outcome"}},
    )
    return result
