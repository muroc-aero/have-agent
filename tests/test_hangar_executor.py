"""HangarExecutor mapping tests against a stubbed hangar.omd.run.

the-hangar is not a dependency of have-agent, so these tests inject a fake
``hangar.omd.run`` module and verify the payload -> run_plan argument
mapping and the run_plan result -> ExecResult classification.
"""

import sys
import types
from pathlib import Path

import pytest

from have_agent.hangar_executor import HangarExecutor

PAYLOAD = {
    "plan_ref": "plans/tbm_series_hybrid.yaml",
    "case_id": "e500_r700",
    "overrides": {
        "battery.specific_energy_whkg": 500,
        "mission.range_nm": 700,
    },
    "warm_start_run": "run-20260703T000000-abcd1234",
}

PARAM_MAP = {
    "battery.specific_energy_whkg":
        "components[acmodel].config.battery.specific_energy_whkg",
    "mission.range_nm": "components[mission].config.mission_range_nm",
}


@pytest.fixture()
def fake_run_plan(monkeypatch):
    """Install a stub hangar.omd.run whose run_plan records its call."""
    calls: list[dict] = []
    response = {
        "run_id": "run-x",
        "status": "completed",
        "summary": {"mtow_kg": 3200.0},
        "errors": [],
    }

    def run_plan(plan_path, **kwargs):
        calls.append({"plan_path": plan_path, **kwargs})
        return dict(response)

    mod_run = types.ModuleType("hangar.omd.run")
    mod_run.run_plan = run_plan
    mod_omd = types.ModuleType("hangar.omd")
    mod_omd.run = mod_run
    mod_hangar = types.ModuleType("hangar")
    mod_hangar.omd = mod_omd
    for name, mod in (
        ("hangar", mod_hangar), ("hangar.omd", mod_omd), ("hangar.omd.run", mod_run)
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return calls, response


def test_argument_mapping(fake_run_plan):
    calls, _ = fake_run_plan
    ex = HangarExecutor("/deploy/hangar", PARAM_MAP, timeout_s=1200)
    res = ex.execute(PAYLOAD, study_id="S1", job_id="J1", attempt=2)
    assert res.ok and res.run_ref == "run-x"
    assert res.result == {"mtow_kg": 3200.0}
    (call,) = calls
    assert call["plan_path"] == Path("/deploy/hangar/plans/tbm_series_hybrid.yaml")
    assert call["case_id"] == "e500_r700"
    assert call["study_id"] == "S1"
    assert call["attempt"] == 2
    assert call["warm_start_run"] == PAYLOAD["warm_start_run"]
    assert call["timeout_seconds"] == 1200
    assert call["overrides"] == {
        "components[acmodel].config.battery.specific_energy_whkg": 500,
        "components[mission].config.mission_range_nm": 700,
    }


def test_baked_plan_overrides_win_over_param_map(fake_run_plan):
    # a study with a bind: section arrives pre-translated (DECISIONS #26);
    # param_map is the fallback for studies without one
    calls, _ = fake_run_plan
    ex = HangarExecutor(param_map=PARAM_MAP)
    baked = {"components[mission].config.mission_params.mission_range_NM": 700}
    payload = dict(PAYLOAD, plan_overrides=baked)
    ex.execute(payload, study_id="S1", job_id="J1", attempt=1)
    assert calls[0]["overrides"] == baked


def test_unmapped_override_keys_pass_through(fake_run_plan):
    calls, _ = fake_run_plan
    ex = HangarExecutor()
    payload = dict(PAYLOAD, overrides={"components[m].config.x": 1.0})
    ex.execute(payload, study_id="S1", job_id="J1", attempt=1)
    assert calls[0]["overrides"] == {"components[m].config.x": 1.0}
    assert calls[0]["plan_path"] == Path("plans/tbm_series_hybrid.yaml")


def test_solver_failure_is_transient(fake_run_plan):
    _, response = fake_run_plan
    response.update(
        status="failed",
        errors=[{"path": "execute", "message": "Newton did not converge"}],
    )
    res = HangarExecutor().execute(PAYLOAD, study_id="S1", job_id="J1", attempt=1)
    assert not res.ok
    assert not res.permanent
    assert "Newton did not converge" in res.error
    assert res.run_ref == "run-x"


def test_timeout_is_transient(fake_run_plan):
    _, response = fake_run_plan
    response.update(
        status="timeout",
        errors=[{"path": "execute", "message": "Wallclock timeout after 600s"}],
    )
    res = HangarExecutor().execute(PAYLOAD, study_id="S1", job_id="J1", attempt=1)
    assert not res.ok and not res.permanent


def test_invalid_plan_is_permanent(fake_run_plan):
    _, response = fake_run_plan
    response.update(
        run_id=None,
        status="failed",
        errors=[{"path": "overrides", "message": "no element with id 'nope'"}],
    )
    res = HangarExecutor().execute(PAYLOAD, study_id="S1", job_id="J1", attempt=1)
    assert not res.ok
    assert res.permanent
    assert res.run_ref is None


def test_materialize_failure_is_permanent(fake_run_plan):
    _, response = fake_run_plan
    response.update(
        status="failed",
        errors=[{"path": "materialize", "message": "unknown component type"}],
    )
    res = HangarExecutor().execute(PAYLOAD, study_id="S1", job_id="J1", attempt=1)
    assert not res.ok and res.permanent


def test_idempotent_replay_maps_like_a_fresh_result(fake_run_plan):
    _, response = fake_run_plan
    response["idempotent"] = True
    res = HangarExecutor().execute(PAYLOAD, study_id="S1", job_id="J1", attempt=1)
    assert res.ok and res.run_ref == "run-x"
