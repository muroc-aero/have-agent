"""RangeSafetyCheckSuite: parity predicate, plausibility fences, verdict fold.

the-hangar is not a dependency; the suite's lazy imports are stubbed via
sys.modules (same pattern as test_hangar_executor).
"""

import sys
import types

import pytest

from have_agent.checks import (
    DEFAULT_AXIS_MAP,
    RangeSafetyCheckSuite,
    fold,
    is_parity,
    load_reference,
    plausibility_raymer_breguet_v1,
)

REF_CSV = """\
range_nm,e_batt_whkg,mtow_kg,fuel_burn_kg,mtow_sigma_kg,fuel_sigma_kg,mtow_at_bound,fuel_check,source
500,250,4042.7,342.0,0.0,0.0,0,strict,paper-table-4
500,500,5700.0,235.9,0.0,0.0,1,advisory,paper-table-4
500,750,5672.4,0.0,0.0,0.0,0,skip,paper-table-4
300,250,3849.7,204.1,128.4,20.4,0,strict,figure-pixel
"""

OVERRIDES = {
    "battery.specific_energy_whkg": 250,
    "mission.range_nm": 500,
}

ACCEPTANCE = {
    "parity": {
        "reference": "refs/test_ref.csv",
        "metric": "is_parity",
        "tolerances": {"mtow_kg": 0.03, "fuel_burn_kg": 0.05},
    },
    "convergence_rate_min": 0.90,
    "plausibility_suite": "raymer_breguet_v1",
}


@pytest.fixture()
def ref_root(tmp_path):
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "test_ref.csv").write_text(REF_CSV)
    return tmp_path


@pytest.fixture()
def fake_hangar(monkeypatch):
    """Stub hangar.range_safety.assertions + hangar.results_reader."""
    state = {
        "convergence": {"passed": True, "checks": [
            {"name": "run_exists", "passed": True, "message": "found"},
            {"name": "no_nan_values", "passed": True, "message": "clean"},
        ], "summary": "ok"},
        "case_data": {
            "ac|weights|MTOW": 4050.0,
            "descent.fuel_used_final": 345.0,
            "ac|weights|W_battery": 150.0,
        },
        "db_paths": [],
    }

    def assert_convergence(run_id, db_path=None):
        state["db_paths"].append(db_path)
        return dict(state["convergence"])

    def init_analysis_db(db_path=None):
        state["db_paths"].append(db_path)

    def query_run_results(run_id, variables=None):
        return [{"iteration": 0, "case_type": "final", "data": dict(state["case_data"])}]

    def resolve_scalar(data, name):
        return data.get(name)

    mod_assert = types.ModuleType("hangar.range_safety.assertions")
    mod_assert.assert_convergence = assert_convergence
    mod_rs = types.ModuleType("hangar.range_safety")
    mod_rs.assertions = mod_assert
    mod_reader = types.ModuleType("hangar.results_reader")
    mod_reader.init_analysis_db = init_analysis_db
    mod_reader.query_run_results = query_run_results
    mod_reader.resolve_scalar = resolve_scalar
    mod_hangar = types.ModuleType("hangar")
    mod_hangar.range_safety = mod_rs
    mod_hangar.results_reader = mod_reader
    for name, mod in (
        ("hangar", mod_hangar),
        ("hangar.range_safety", mod_rs),
        ("hangar.range_safety.assertions", mod_assert),
        ("hangar.results_reader", mod_reader),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return state


class TestPredicates:
    def test_load_reference_parses_floats(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        assert table[(500.0, 250.0)]["mtow_kg"] == 4042.7
        assert table[(300.0, 250.0)]["fuel_check"] == "strict"

    def test_parity_pass_within_tolerance(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        checks = is_parity(
            {"mtow_kg": 4100.0, "fuel_burn_kg": 350.0},
            table[(500.0, 250.0)], {"mtow_kg": 0.03, "fuel_burn_kg": 0.05},
        )
        assert [c["level"] for c in checks] == ["pass", "pass"]

    def test_parity_mtow_miss_fails(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        checks = is_parity({"mtow_kg": 5000.0, "fuel_burn_kg": 342.0},
                           table[(500.0, 250.0)], {"mtow_kg": 0.03})
        assert checks[0]["check"] == "parity.mtow_kg"
        assert checks[0]["level"] == "fail"

    def test_parity_fuel_miss_advisory_warns_on_flat_ridge(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        # the-hangar reproduction burns ~99 kg where the paper burns 236 kg
        checks = is_parity({"mtow_kg": 5700.0, "fuel_burn_kg": 99.0},
                           table[(500.0, 500.0)], {"fuel_burn_kg": 0.05})
        fuel = next(c for c in checks if c["check"] == "parity.fuel_burn_kg")
        assert fuel["level"] == "warn"

    def test_parity_fuel_skipped_when_all_electric(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        checks = is_parity({"mtow_kg": 5672.0, "fuel_burn_kg": 0.0},
                           table[(500.0, 750.0)], {"fuel_burn_kg": 0.05})
        fuel = next(c for c in checks if c["check"] == "parity.fuel_burn_kg")
        assert fuel["level"] == "pass"
        assert "skip" in fuel["detail"]

    def test_parity_tolerance_widened_by_digitization_sigma(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        row = table[(300.0, 250.0)]  # sigma 128.4 kg on 3849.7 -> eff tol 6.7%
        checks = is_parity({"mtow_kg": 3849.7 * 1.05, "fuel_burn_kg": 204.1},
                           row, {"mtow_kg": 0.03})
        assert checks[0]["level"] == "pass"
        assert "6.7%" in checks[0]["detail"]

    def test_parity_missing_output_is_error(self, ref_root):
        table = load_reference(ref_root / "refs" / "test_ref.csv")
        checks = is_parity({}, table[(500.0, 250.0)], {})
        assert checks[0]["level"] == "error"

    def test_plausibility_fences(self):
        good = plausibility_raymer_breguet_v1(
            {"mtow_kg": 4000.0, "fuel_burn_kg": 300.0, "battery_mass_kg": 200.0},
            {DEFAULT_AXIS_MAP["range_nm"]: 500},
        )
        assert all(c["level"] == "pass" for c in good)
        bad = plausibility_raymer_breguet_v1(
            {"mtow_kg": 40000.0, "fuel_burn_kg": 3000.0, "battery_mass_kg": 50000.0},
            {DEFAULT_AXIS_MAP["range_nm"]: 500},
        )
        assert {c["check"] for c in bad if c["level"] == "fail"} == {
            "plausibility.mtow_fence",
            "plausibility.battery_lt_mtow",
            "plausibility.breguet_mileage",
        }

    def test_fold_worst_wins(self):
        assert fold(["pass", "warn", "pass"]) == "warn"
        assert fold(["pass", "fail", "warn"]) == "fail"
        assert fold(["error", "fail"]) == "error"
        assert fold([]) == "pass"


class TestRangeSafetyCheckSuite:
    def test_converged_in_tolerance_passes(self, ref_root, fake_hangar):
        suite = RangeSafetyCheckSuite(ref_root, db_path=ref_root / "muroc.db")
        level, checks = suite.run("e250_r500", "run-1", ACCEPTANCE, overrides=OVERRIDES)
        assert level == "pass"
        names = {c["check"] for c in checks}
        assert "convergence.run_exists" in names
        assert "parity.mtow_kg" in names
        assert "plausibility.breguet_mileage" in names
        assert fake_hangar["db_paths"][0] == ref_root / "muroc.db"

    def test_no_run_ref_is_error(self, ref_root, fake_hangar):
        suite = RangeSafetyCheckSuite(ref_root)
        level, checks = suite.run("e250_r500", None, ACCEPTANCE, overrides=OVERRIDES)
        assert level == "error"
        assert checks[0]["check"] == "run_ref_present"

    def test_non_converged_fails_and_skips_parity(self, ref_root, fake_hangar):
        fake_hangar["convergence"] = {
            "passed": False,
            "checks": [{"name": "no_nan_values", "passed": False, "message": "NaN"}],
            "summary": "bad",
        }
        suite = RangeSafetyCheckSuite(ref_root)
        level, checks = suite.run("e250_r500", "run-1", ACCEPTANCE, overrides=OVERRIDES)
        assert level == "fail"
        assert all(not c["check"].startswith("parity") for c in checks)

    def test_parity_failure_folds_to_fail(self, ref_root, fake_hangar):
        fake_hangar["case_data"]["ac|weights|MTOW"] = 5000.0  # ref 4042.7, 24% off
        suite = RangeSafetyCheckSuite(ref_root)
        level, checks = suite.run("e250_r500", "run-1", ACCEPTANCE, overrides=OVERRIDES)
        assert level == "fail"
        mtow = next(c for c in checks if c["check"] == "parity.mtow_kg")
        assert mtow["level"] == "fail"

    def test_missing_reference_cell_warns(self, ref_root, fake_hangar):
        suite = RangeSafetyCheckSuite(ref_root)
        overrides = {**OVERRIDES, "mission.range_nm": 999}
        level, checks = suite.run("e250_r999", "run-1", ACCEPTANCE, overrides=overrides)
        assert level == "warn"
        parity = next(c for c in checks if c["check"] == "parity")
        assert "no reference cell" in parity["detail"]

    def test_no_parity_block_runs_convergence_only(self, ref_root, fake_hangar):
        suite = RangeSafetyCheckSuite(ref_root)
        level, checks = suite.run(
            "e250_r500", "run-1", {"plausibility_suite": "none"}, overrides=OVERRIDES)
        assert level == "pass"
        assert all(
            c["check"].startswith("convergence") or c["check"] == "outputs"
            for c in checks
        )

    def test_outputs_channel_carries_resolved_values(self, ref_root, fake_hangar):
        suite = RangeSafetyCheckSuite(ref_root)
        _, checks = suite.run("e250_r500", "run-1", ACCEPTANCE, overrides=OVERRIDES)
        out = next(c for c in checks if c["check"] == "outputs")
        assert out["outputs"] == {
            "mtow_kg": 4050.0, "fuel_burn_kg": 345.0, "battery_mass_kg": 150.0,
        }
        assert out["level"] == "pass"