"""REPORT execution: study briefing artifact (markdown, canned template) +
report.published event + study.conclusion_ref. Deterministic v0; the LLM
narrative hook comes later."""

import json
import sqlite3
from pathlib import Path

from have_agent.db import utcnow
from have_agent.substrate import emit_event

AGENT = "agent:have"


def _metrics(conn: sqlite3.Connection, study_id: str) -> dict:
    """MVP metrics (§9) computable from the substrate alone."""
    submitted = conn.execute(
        "SELECT ts FROM event WHERE object_id = ? AND verb = 'study.submitted'",
        (study_id,),
    ).fetchone()
    failed_jobs = {
        r["object_id"]
        for r in conn.execute(
            "SELECT DISTINCT object_id FROM event e JOIN job j ON j.id = e.object_id"
            " WHERE e.verb = 'job.failed' AND j.study_id = ?",
            (study_id,),
        )
    }
    recovered = conn.execute(
        "SELECT COUNT(*) FROM job WHERE study_id = ? AND parent_job_id IS NOT NULL"
        " AND type = 'ANALYSIS' AND state IN ('succeeded', 'accepted')",
        (study_id,),
    ).fetchone()[0]
    human_events = conn.execute(
        "SELECT COUNT(*) FROM event e JOIN job j ON j.id = e.object_id"
        " WHERE j.study_id = ? AND e.actor LIKE 'human:%'",
        (study_id,),
    ).fetchone()[0]
    return {
        "submitted_at": submitted["ts"] if submitted else None,
        "report_at": utcnow(),
        "failed_jobs": len(failed_jobs),
        "recovered_jobs": recovered,
        "recovery_rate": round(recovered / len(failed_jobs), 3) if failed_jobs else None,
        "human_job_events": human_events,
    }


def build_report(
    conn: sqlite3.Connection, report_job: sqlite3.Row, artifacts_dir: Path
) -> Path:
    study_id = report_job["study_id"]
    study = conn.execute("SELECT * FROM study WHERE id = ?", (study_id,)).fetchone()
    payload = json.loads(report_job["payload_json"])
    counts = conn.execute(
        "SELECT type, state, COUNT(*) AS n FROM job WHERE study_id = ?"
        " GROUP BY type, state ORDER BY type, state",
        (study_id,),
    ).fetchall()
    results = conn.execute(
        "SELECT j.payload_json, j.run_ref, j.state, v.level, v.checks_json FROM job j"
        " LEFT JOIN verdict v ON v.id = j.verdict_id"
        " WHERE j.study_id = ? AND j.type = 'ANALYSIS'"
        " AND j.state IN ('accepted', 'succeeded', 'review') ORDER BY j.created_at",
        (study_id,),
    ).fetchall()
    triage_notes = conn.execute(
        "SELECT e.object_id, e.payload_json FROM event e JOIN job j ON j.id = e.object_id"
        " WHERE e.verb = 'decision.logged' AND j.study_id = ? ORDER BY e.ts",
        (study_id,),
    ).fetchall()
    metrics = _metrics(conn, study_id)

    lines = [
        f"# {study['title']}",
        "",
        f"- study: `{study_id}`",
        f"- template: `{payload.get('template', 'study_briefing_v1')}`",
        f"- generated: {metrics['report_at']}",
        "",
        "## Job summary",
        "",
        "| type | state | count |",
        "|---|---|---|",
        *(f"| {c['type']} | {c['state']} | {c['n']} |" for c in counts),
        "",
        "## Case results",
        "",
        "| case | run_ref | state | verdict | mtow_kg | fuel_burn_kg | battery_mass_kg |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        case = json.loads(r["payload_json"])
        # physical outputs come from the CHECK verdict's structured
        # 'outputs' entry (stamped onto the ANALYSIS job at gating)
        outputs = {}
        for c in json.loads(r["checks_json"]) if r["checks_json"] else []:
            if c.get("check") == "outputs":
                outputs = c.get("outputs") or {}
        cells = " | ".join(
            f"{outputs[k]:.1f}" if k in outputs else ""
            for k in ("mtow_kg", "fuel_burn_kg", "battery_mass_kg")
        )
        lines.append(
            f"| {case['case_id']} | {r['run_ref'] or '—'} | {r['state']}"
            f" | {r['level'] or ''} | {cells} |"
        )
    lines += [
        "",
        "## Triage summary",
        "",
        *(
            f"- `{t['object_id']}`: {json.loads(t['payload_json']).get('decision', '?')}"
            f" — {json.loads(t['payload_json']).get('reason', '')}"
            for t in triage_notes
        ),
        "",
        "## Metrics",
        "",
        "```json",
        json.dumps(metrics, indent=2),
        "```",
        "",
    ]

    out_dir = artifacts_dir / study_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{payload.get('template', 'study_briefing_v1')}.md"
    out_path.write_text("\n".join(lines))

    conn.execute(
        "UPDATE study SET conclusion_ref = ?, updated_at = ? WHERE id = ?",
        (str(out_path), utcnow(), study_id),
    )
    emit_event(
        conn, AGENT, "report.published", "study", study_id,
        {"artifact": str(out_path), "report_job_id": report_job["id"], "metrics": metrics},
    )
    return out_path
