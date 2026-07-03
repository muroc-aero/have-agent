"""DECOMPOSE: Brelje StudyRequest YAML -> case matrix -> proposed jobs."""

import json

import pytest

from have_agent.decompose import case_matrix, parse_study_request, short_names, submit_study
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
