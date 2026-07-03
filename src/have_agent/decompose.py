"""DECOMPOSE: StudyRequest YAML -> case matrix -> proposed jobs + plan proposal.

Deterministic v0. The LLM hook (plan annotation/reordering, e.g. warm-start
ordering) is stubbed: set llm_plan_hook to a callable and its output lands in
plan_proposal_json alongside a decision.logged event.
"""

import itertools
import sqlite3
from collections.abc import Callable
from typing import Any

import yaml

from have_agent.substrate import create_job, emit_event, study_transition

AGENT = "agent:have"

# unit suffixes stripped when deriving case-id short names ("e500_r700")
_UNIT_TOKENS = {"whkg", "nm", "kg", "km", "kw", "kwh", "hr", "h", "s", "m", "mb", "pct"}

# Optional hook: (spec: dict, cases: list[dict]) -> dict of plan annotations.
llm_plan_hook: Callable[[dict, list[dict]], dict] | None = None


def short_names(keys: list[str]) -> dict[str, str]:
    """Deterministic short alias per sweep axis: last meaningful word's first
    letter(s) of the final dotted segment, unit suffix stripped
    (battery.specific_energy_whkg -> 'e', mission.range_nm -> 'r').
    Collisions extend the prefix, then fall back to an index."""
    shorts: dict[str, str] = {}
    for i, key in enumerate(keys):
        words = key.split(".")[-1].split("_")
        if len(words) > 1 and words[-1].lower() in _UNIT_TOKENS:
            words = words[:-1]
        word = words[-1].lower()
        alias = next(
            (word[:n] for n in range(1, len(word) + 1) if word[:n] not in shorts.values()),
            f"{word}{i}",
        )
        shorts[key] = alias
    return shorts


def _fmt_value(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace(".", "p").replace("-", "m")


def case_matrix(sweep: dict[str, list]) -> list[dict[str, Any]]:
    """Cartesian product of sweep axes, row-major in YAML axis order.
    Each case: {'case_id': 'e500_r700', 'overrides': {...}}."""
    keys = list(sweep)
    shorts = short_names(keys)
    cases = []
    for values in itertools.product(*(sweep[k] for k in keys)):
        overrides = dict(zip(keys, values, strict=True))
        case_id = "_".join(f"{shorts[k]}{_fmt_value(v)}" for k, v in overrides.items())
        cases.append({"case_id": case_id, "overrides": overrides})
    return cases


def parse_study_request(yaml_text: str) -> dict[str, Any]:
    spec = yaml.safe_load(yaml_text)
    if not isinstance(spec, dict):
        raise ValueError("StudyRequest YAML must be a mapping")
    for key in ("study", "baseline", "sweep"):
        if key not in spec:
            raise ValueError(f"StudyRequest missing required key {key!r}")
    if "template" not in spec["baseline"]:
        raise ValueError("StudyRequest baseline missing 'template'")
    return spec


def decompose_study(conn: sqlite3.Connection, study_id: str) -> dict[str, Any]:
    """Expand the study's sweep into ANALYSIS/CHECK/REPORT jobs (all
    'proposed'), attach plan_proposal_json, move study draft -> proposed."""
    study = conn.execute("SELECT * FROM study WHERE id = ?", (study_id,)).fetchone()
    spec = parse_study_request(study["intent_yaml"])
    policy = spec.get("policy", {})
    acceptance = spec.get("acceptance", {})
    plan_ref = spec["baseline"]["template"]
    solver = plan_ref.split("/", 1)[0]
    priority = int(policy.get("priority", 50))
    # attempts = first run + policy-bounded auto-retries
    max_attempts = int(policy.get("auto_retry_max", 2)) + 1
    cases = case_matrix(spec["sweep"])

    check_ids = []
    for case in cases:
        analysis_id = create_job(
            conn, study_id, "ANALYSIS",
            payload={
                "plan_ref": plan_ref,
                "case_id": case["case_id"],
                "overrides": case["overrides"],
                "warm_start_run": None,
            },
            actor=AGENT,
            priority=priority,
            resource={"est_runtime_s": 240, "requires": [solver]},
            max_attempts=max_attempts,
        )
        check_ids.append(
            create_job(
                conn, study_id, "CHECK",
                payload={
                    "run_ref": None,  # filled from dep at dispatch
                    "check_suite": acceptance.get("plausibility_suite", "default_v0"),
                    "acceptance": acceptance,
                },
                actor=AGENT,
                priority=priority,
                resource={"est_runtime_s": 30},
                max_attempts=max_attempts,
                deps=[analysis_id],
            )
        )
    report_id = create_job(
        conn, study_id, "REPORT",
        payload={
            "template": policy.get("report", "study_briefing_v1"),
            "include": ["carpet_plot", "parity_table", "triage_summary", "metrics"],
        },
        actor=AGENT,
        priority=priority,
        resource={"est_runtime_s": 60},
        deps=check_ids,
    )

    plan_proposal: dict[str, Any] = {
        "cases": cases,
        "job_counts": {"ANALYSIS": len(cases), "CHECK": len(check_ids), "REPORT": 1},
        "report_job_id": report_id,
        "case_order": "sweep row-major (YAML axis order)",
        "retry_strategy": policy.get("retry_strategy", "none"),
        "decided_by": "deterministic-v0",
    }
    if llm_plan_hook is not None:
        annotations = llm_plan_hook(spec, cases)
        plan_proposal["llm_annotations"] = annotations
        emit_event(
            conn, AGENT, "decision.logged", "study", study_id,
            {"decision": "plan_annotations", "annotations": annotations},
        )
    study_transition(
        conn, study_id, "proposed", AGENT,
        {"job_count": 2 * len(cases) + 1},
        plan_proposal=plan_proposal,
        expected_status="draft",
    )
    return plan_proposal


def submit_study(
    conn: sqlite3.Connection, yaml_text: str, default_owner: str
) -> tuple[str, dict[str, Any]]:
    """CLI submit: create the study from YAML and run DECOMPOSE.
    Returns (study_id, plan_proposal)."""
    from have_agent.substrate import create_study

    spec = parse_study_request(yaml_text)
    study_id = create_study(
        conn,
        title=spec.get("title", spec["study"]),
        intent_yaml=yaml_text,
        owner=spec.get("owner", default_owner),
        policy=spec.get("policy", {}),
        actor=AGENT,
    )
    return study_id, decompose_study(conn, study_id)
