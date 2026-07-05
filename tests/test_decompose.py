"""DECOMPOSE: Brelje StudyRequest YAML -> case matrix -> proposed jobs."""

import json

import pytest

from have_agent.decompose import (
    build_cases,
    case_matrix,
    parse_study_request,
    short_names,
    submit_study,
)
from tests.conftest import study_row

BRELJE_YAML = """\
study: brelje_replication
title: Series-hybrid e_batt vs range sweep (Brelje & Martins replication)
owner: human:alex
baseline:
  template: ocp/tbm_series_hybrid
sweep:
  battery.specific_energy_whkg: [300, 400, 500, 600, 700, 800]
  mission.range_nm: [300, 400, 500, 600, 700, 800, 900, 1000]
outputs: [mtow_kg, fuel_burn_kg, battery_mass_kg, converged]
acceptance:
  parity:
    reference: refs/brelje_fig_digitized.csv
    metric: is_parity
    tolerances: {mtow_kg: 0.03, fuel_burn_kg: 0.05}
  convergence_rate_min: 0.90
  plausibility_suite: raymer_breguet_v1
policy:
  priority: 50
  compute_budget: {max_wall_hours: 6, workers: any}
  auto_retry_max: 2
  retry_strategy: warm_start_nearest_converged
  auto_accept: {verdict_level: pass}
  gate_on: [warn, fail]
  report: study_briefing_v1
"""


def test_short_names_strip_units():
    shorts = short_names(["battery.specific_energy_whkg", "mission.range_nm"])
    assert shorts == {
        "battery.specific_energy_whkg": "e",
        "mission.range_nm": "r",
    }


def test_short_names_collision_extends_prefix():
    shorts = short_names(["a.range_nm", "b.radius_km"])
    assert shorts == {"a.range_nm": "r", "b.radius_km": "ra"}


def test_case_matrix_brelje():
    spec = parse_study_request(BRELJE_YAML)
    cases = case_matrix(spec["sweep"])
    assert len(cases) == 48
    assert cases[0] == {
        "case_id": "e300_r300",
        "overrides": {
            "battery.specific_energy_whkg": 300,
            "mission.range_nm": 300,
        },
    }
    assert cases[-1]["case_id"] == "e800_r1000"
    assert len({c["case_id"] for c in cases}) == 48


def test_parse_rejects_incomplete_request():
    with pytest.raises(ValueError, match="sweep"):
        parse_study_request("study: x\nbaseline: {template: t}\n")


# bind: baked plan-path overrides + cases: explicit case list (DECISIONS #26/#27)

BIND_CASES_YAML = """\
study: bind_cases
owner: human:alex
baseline:
  template: ocp/kingair.yaml
bind:
  mission.range_nm: components[mission].config.mission_params.mission_range_NM
  battery.specific_energy_whkg:
    - components[mission].config.mission_params.battery_specific_energy
    - components[mission].config.propulsion_overrides.battery_specific_energy
sweep:
  battery.specific_energy_whkg: [400, 500]
  mission.range_nm: [500, 700]
cases:
  - overrides: {battery.specific_energy_whkg: 475, mission.range_nm: 625}
  - case_id: probe_high
    overrides: {battery.specific_energy_whkg: 800, mission.range_nm: 1000}
"""


def test_parse_accepts_cases_without_sweep():
    spec = parse_study_request(
        "study: x\nbaseline: {template: ocp/t}\n"
        "cases:\n  - overrides: {a.b_nm: 1}\n"
    )
    assert build_cases(spec) == [{"case_id": "b1", "overrides": {"a.b_nm": 1}}]


@pytest.mark.parametrize(("snippet", "match"), [
    ("bind: [not, a, map]\nsweep: {a: [1]}\n", "bind"),
    ("bind: {k: []}\nsweep: {a: [1]}\n", "bind"),
    ("cases: {not: a-list}\n", "cases"),
    ("cases:\n  - {case_id: x}\n", "overrides"),
    ("cases:\n  - {case_id: '', overrides: {a: 1}}\n", "case_id"),
])
def test_parse_rejects_malformed_bind_and_cases(snippet, match):
    with pytest.raises(ValueError, match=match):
        parse_study_request(f"study: x\nbaseline: {{template: t}}\n{snippet}")


def test_build_cases_appends_explicit_after_sweep():
    spec = parse_study_request(BIND_CASES_YAML)
    cases = build_cases(spec)
    assert [c["case_id"] for c in cases] == [
        "e400_r500", "e400_r700", "e500_r500", "e500_r700",  # sweep row-major
        "e475_r625",     # derived id, same scheme as sweep cases
        "probe_high",    # explicit id kept verbatim
    ]


def test_build_cases_rejects_duplicate_ids():
    spec = parse_study_request(
        BIND_CASES_YAML + "  - overrides: {battery.specific_energy_whkg: 400,"
                          " mission.range_nm: 500}\n"
    )
    with pytest.raises(ValueError, match="duplicate case_id.*e400_r500"):
        build_cases(spec)


def test_submit_bakes_bound_plan_overrides(conn):
    study_id, plan = submit_study(conn, BIND_CASES_YAML, "human:alex")
    assert plan["case_sources"] == {"sweep": 4, "explicit": 2}
    assert plan["bound"] is True
    payloads = [
        json.loads(r["payload_json"]) for r in conn.execute(
            "SELECT payload_json FROM job WHERE study_id = ? AND type = 'ANALYSIS'"
            " ORDER BY created_at",
            (study_id,),
        ).fetchall()
    ]
    first = payloads[0]
    # domain-keyed overrides stay for parity lookups / case identity ...
    assert first["overrides"] == {
        "battery.specific_energy_whkg": 400, "mission.range_nm": 500,
    }
    # ... and the plan-path translation rides alongside, fan-out included
    assert first["plan_overrides"] == {
        "components[mission].config.mission_params.battery_specific_energy": 400,
        "components[mission].config.propulsion_overrides.battery_specific_energy": 400,
        "components[mission].config.mission_params.mission_range_NM": 500,
    }
    assert all("plan_overrides" in p for p in payloads)


def test_submit_without_bind_bakes_no_plan_overrides(conn):
    _, _ = submit_study(conn, BRELJE_YAML, "human:alex")
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM job WHERE type = 'ANALYSIS' LIMIT 1"
    ).fetchone()["payload_json"])
    assert "plan_overrides" not in payload  # --param-map path still applies


def test_submit_study_creates_jobs_and_proposes(conn):
    study_id, plan = submit_study(conn, BRELJE_YAML, "human:fallback")
    study = study_row(conn, study_id)
    assert study["status"] == "proposed"
    assert study["owner"] == "human:alex"  # from YAML, not fallback
    assert study["intent_yaml"] == BRELJE_YAML
    assert json.loads(study["policy_json"])["auto_retry_max"] == 2
    assert json.loads(study["plan_proposal_json"])["job_counts"] == {
        "ANALYSIS": 48, "CHECK": 48, "REPORT": 1,
    }

    jobs = conn.execute(
        "SELECT type, state, COUNT(*) AS n FROM job WHERE study_id = ?"
        " GROUP BY type, state",
        (study_id,),
    ).fetchall()
    assert {(j["type"], j["state"], j["n"]) for j in jobs} == {
        ("ANALYSIS", "proposed", 48), ("CHECK", "proposed", 48), ("REPORT", "proposed", 1),
    }

    # every CHECK depends on exactly its ANALYSIS; REPORT depends on all CHECKs
    analysis = conn.execute(
        "SELECT payload_json FROM job WHERE study_id = ? AND type = 'ANALYSIS'"
        " ORDER BY created_at LIMIT 1",
        (study_id,),
    ).fetchone()
    payload = json.loads(analysis["payload_json"])
    assert payload["plan_ref"] == "ocp/tbm_series_hybrid"
    assert payload["case_id"] == "e300_r300"
    assert payload["warm_start_run"] is None

    report_deps = conn.execute(
        "SELECT COUNT(*) FROM job_dep d JOIN job j ON j.id = d.job_id"
        " WHERE j.type = 'REPORT' AND j.study_id = ?",
        (study_id,),
    ).fetchone()[0]
    assert report_deps == 48

    # attempts budget = 1 + auto_retry_max
    assert conn.execute(
        "SELECT DISTINCT max_attempts FROM job WHERE study_id = ?"
        " AND type = 'ANALYSIS'",
        (study_id,),
    ).fetchone()[0] == 3
