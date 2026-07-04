"""Real CHECK suite: range-safety assertions + parity vs digitized reference.

Spec §8.5: v0 runs range-safety checks in-process on the worker after the
ANALYSIS job, still recorded as a separate CHECK job + verdict. the-hangar
is imported lazily (same posture as HangarExecutor): have-agent carries no
hard dependency, tests stub the modules.

Three check families, folded into one verdict level:

* convergence  -- hangar.range_safety.assertions.assert_convergence on the
  upstream run (run exists, case data present, no NaNs, residuals).
* parity       -- the §5 acceptance predicate (``metric: is_parity``):
  run outputs vs refs/brelje_fig_digitized.csv at the case's sweep
  coordinates, relative tolerances from acceptance.parity.tolerances.
  Reference rows carry digitization uncertainty and a per-row
  ``fuel_check`` class (strict | advisory | skip): fuel misses in the
  flat-objective-ridge region warn instead of fail (see refs/README.md).
* plausibility -- raymer_breguet_v1: unit-level sanity fences (weights
  positive and ordered, fuel consistent with a Breguet-style bound) that
  catch a solver converging to garbage before parity flags it.

Verdict levels fold worst-first: error > fail > warn > pass.
"""

import csv
from pathlib import Path
from typing import Any

_LEVEL_ORDER = {"pass": 0, "warn": 1, "fail": 2, "error": 3}

# outputs the acceptance block names -> scalar names in the run's case data
# (defaults fit the Brelje King Air OCP plans; override per deployment)
DEFAULT_OUTPUT_MAP = {
    "mtow_kg": "ac|weights|MTOW",
    "fuel_burn_kg": "descent.fuel_used_final",
    "battery_mass_kg": "ac|weights|W_battery",
}

# sweep-axis keys in the case overrides -> reference CSV coordinate columns
DEFAULT_AXIS_MAP = {
    "range_nm": "mission.range_nm",
    "e_batt_whkg": "battery.specific_energy_whkg",
}

# raymer_breguet_v1 fences (King Air C90GT series-hybrid retrofit class)
_MTOW_FENCE_KG = (2000.0, 7000.0)   # bound is 5700; fence catches unit slips
_BREGUET_MAX_KG_PER_NM = 2.0        # fuel/range above this is not this aircraft


def fold(levels: list[str]) -> str:
    return max(levels, key=lambda lv: _LEVEL_ORDER.get(lv, 3), default="pass")


def load_reference(path: str | Path) -> dict[tuple[float, float], dict[str, Any]]:
    """Reference CSV -> {(range_nm, e_batt_whkg): row} with floats parsed."""
    table = {}
    with Path(path).open(newline="") as f:
        for row in csv.DictReader(f):
            key = (float(row["range_nm"]), float(row["e_batt_whkg"]))
            table[key] = {
                **row,
                "mtow_kg": float(row["mtow_kg"]),
                "fuel_burn_kg": float(row["fuel_burn_kg"]),
                "mtow_sigma_kg": float(row.get("mtow_sigma_kg") or 0.0),
                "fuel_sigma_kg": float(row.get("fuel_sigma_kg") or 0.0),
            }
    return table


def _rel_check(name: str, got: float, ref: float, sigma: float, tol: float,
               miss_level: str) -> dict[str, Any]:
    """One relative-tolerance comparison, widened by digitization uncertainty."""
    eff_tol = max(tol, 2.0 * sigma / ref) if ref else tol
    rel = abs(got - ref) / ref if ref else float("inf")
    level = "pass" if rel <= eff_tol else miss_level
    return {
        "check": name, "level": level,
        "detail": f"{got:.1f} vs ref {ref:.1f} ({rel:.1%}, tol {eff_tol:.1%})",
    }


def is_parity(outputs: dict[str, float], ref_row: dict[str, Any],
              tolerances: dict[str, float]) -> list[dict[str, Any]]:
    """The §5 parity predicate for one case. Returns check dicts."""
    checks = []
    mtow = outputs.get("mtow_kg")
    if mtow is None:
        checks.append({"check": "parity.mtow_kg", "level": "error",
                       "detail": "mtow_kg not resolvable in run outputs"})
    else:
        checks.append(_rel_check(
            "parity.mtow_kg", mtow, ref_row["mtow_kg"], ref_row["mtow_sigma_kg"],
            float(tolerances.get("mtow_kg", 0.03)), "fail"))

    fuel_mode = ref_row.get("fuel_check", "strict")
    fuel = outputs.get("fuel_burn_kg")
    if fuel_mode == "skip":
        checks.append({"check": "parity.fuel_burn_kg", "level": "pass",
                       "detail": "skipped: near-all-electric reference cell"})
    elif fuel is None:
        checks.append({"check": "parity.fuel_burn_kg", "level": "warn",
                       "detail": "fuel_burn_kg not resolvable in run outputs"})
    else:
        checks.append(_rel_check(
            "parity.fuel_burn_kg", fuel, ref_row["fuel_burn_kg"],
            ref_row["fuel_sigma_kg"], float(tolerances.get("fuel_burn_kg", 0.05)),
            "fail" if fuel_mode == "strict" else "warn"))
    return checks


def plausibility_raymer_breguet_v1(
    outputs: dict[str, float], overrides: dict[str, Any]
) -> list[dict[str, Any]]:
    """Unit-level sanity fences; loose by design (fail = garbage, not nuance)."""
    checks = []
    mtow = outputs.get("mtow_kg")
    fuel = outputs.get("fuel_burn_kg")
    batt = outputs.get("battery_mass_kg")

    if mtow is not None:
        lo, hi = _MTOW_FENCE_KG
        ok = lo <= mtow <= hi
        checks.append({"check": "plausibility.mtow_fence", "level": "pass" if ok else "fail",
                       "detail": f"MTOW {mtow:.0f} kg vs fence [{lo:.0f}, {hi:.0f}]"})
    if fuel is not None:
        ok = fuel >= -1e-6
        checks.append({"check": "plausibility.fuel_nonneg", "level": "pass" if ok else "fail",
                       "detail": f"fuel burn {fuel:.1f} kg"})
    if batt is not None and mtow is not None:
        ok = 0.0 <= batt < mtow
        checks.append({"check": "plausibility.battery_lt_mtow", "level": "pass" if ok else "fail",
                       "detail": f"battery {batt:.0f} kg vs MTOW {mtow:.0f} kg"})
    range_nm = overrides.get(DEFAULT_AXIS_MAP["range_nm"])
    if fuel is not None and range_nm:
        kg_per_nm = fuel / float(range_nm)
        ok = kg_per_nm <= _BREGUET_MAX_KG_PER_NM
        checks.append({"check": "plausibility.breguet_mileage",
                       "level": "pass" if ok else "fail",
                       "detail": f"{kg_per_nm:.2f} kg/nmi vs max {_BREGUET_MAX_KG_PER_NM}"})
    return checks


class RangeSafetyCheckSuite:
    """In-process CHECK executor reading the-hangar's analysis DB.

    reference_root: directory against which acceptance.parity.reference
        resolves (the deployment checkout; refs/ lives in this repo).
    db_path: the-hangar analysis DB -- with the unified-DB deployment this
        is the same muroc.db file the substrate uses (spec §8.3).
    output_map / axis_map: name translation, deployment config like
        HangarExecutor.param_map (DECISIONS.md #16).
    """

    def __init__(
        self,
        reference_root: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        output_map: dict[str, str] | None = None,
        axis_map: dict[str, str] | None = None,
    ):
        self.reference_root = Path(reference_root) if reference_root else Path.cwd()
        self.db_path = Path(db_path) if db_path else None
        self.output_map = output_map or dict(DEFAULT_OUTPUT_MAP)
        self.axis_map = axis_map or dict(DEFAULT_AXIS_MAP)
        self._reference_cache: dict[Path, dict] = {}

    # -- the-hangar seams (lazy imports; stubbed in tests) --------------
    def _convergence(self, run_ref: str) -> dict[str, Any]:
        from hangar.range_safety.assertions import assert_convergence

        return assert_convergence(run_ref, db_path=self.db_path)

    def _final_outputs(self, run_ref: str) -> dict[str, float]:
        from hangar.results_reader import (
            init_analysis_db,
            query_run_results,
            resolve_scalar,
        )

        init_analysis_db(self.db_path)
        cases = query_run_results(run_ref)
        if not cases:
            return {}
        final = [c for c in cases if c.get("case_type") == "final"]
        data = (final[-1] if final else cases[-1]).get("data") or {}
        outputs = {}
        for name, scalar_name in self.output_map.items():
            value = resolve_scalar(data, scalar_name)
            if value is not None:
                outputs[name] = float(value)
        return outputs

    # --------------------------------------------------------------------
    def _reference(self, ref: str) -> dict[tuple[float, float], dict[str, Any]]:
        path = Path(ref)
        if not path.is_absolute():
            path = self.reference_root / path
        if path not in self._reference_cache:
            self._reference_cache[path] = load_reference(path)
        return self._reference_cache[path]

    def run(
        self,
        case_id: str,
        run_ref: str | None,
        acceptance: dict[str, Any],
        *,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if run_ref is None:
            return "error", [
                {"check": "run_ref_present", "level": "error", "detail": "no upstream run"}
            ]
        overrides = overrides or {}
        checks: list[dict[str, Any]] = []

        # 1. convergence (a non-converged run is a failed case, not an error)
        conv = self._convergence(run_ref)
        for c in conv.get("checks", []):
            checks.append({
                "check": f"convergence.{c.get('name')}",
                "level": "pass" if c.get("passed") else "fail",
                "detail": c.get("message", ""),
            })

        outputs = self._final_outputs(run_ref) if conv.get("passed") else {}
        if outputs:
            # structured channel for the briefing's case table (report.py);
            # level 'pass' so it never moves the verdict fold
            checks.append({
                "check": "outputs", "level": "pass",
                "detail": " ".join(f"{k}={v:.1f}" for k, v in sorted(outputs.items())),
                "outputs": outputs,
            })

        # 2. parity vs reference (only meaningful on a converged run)
        parity_spec = acceptance.get("parity") or {}
        if conv.get("passed") and parity_spec.get("reference"):
            key = tuple(
                float(overrides[self.axis_map[axis]])
                for axis in ("range_nm", "e_batt_whkg")
                if self.axis_map[axis] in overrides
            )
            table = self._reference(parity_spec["reference"])
            ref_row = table.get(key) if len(key) == 2 else None
            if ref_row is None:
                checks.append({"check": "parity", "level": "warn",
                               "detail": f"no reference cell at {key} ({case_id})"})
            else:
                checks.extend(
                    is_parity(outputs, ref_row, parity_spec.get("tolerances") or {}))

        # 3. plausibility fences
        if conv.get("passed") and acceptance.get("plausibility_suite") == "raymer_breguet_v1":
            checks.extend(plausibility_raymer_breguet_v1(outputs, overrides))

        return fold([c["level"] for c in checks]), checks
