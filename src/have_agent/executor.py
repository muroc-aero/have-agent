"""Executor protocol + the v0 fake.

The real the-hangar call — run_plan(plan_ref, overrides, case_id) returning a
run_ref, idempotent on (study_id, case_id, attempt) — drops in as another
Executor implementation later. The fake sleeps, returns a canned result, and
supports injectable failures keyed by case_id.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ExecResult:
    ok: bool
    run_ref: str | None = None
    artifacts: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    permanent: bool = False  # triage skips retries for permanent failures


class Executor(Protocol):
    def execute(
        self, payload: dict[str, Any], *, study_id: str, job_id: str, attempt: int
    ) -> ExecResult: ...


class CheckSuite(Protocol):
    def run(
        self,
        case_id: str,
        run_ref: str | None,
        acceptance: dict[str, Any],
        *,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]: ...


class FakeOCPExecutor:
    """Fakes an OCP case: sleep + canned result + injectable failure.

    fail_attempts: {case_id: n} — the case fails while attempt <= n
    (deterministic across retries, since a retry carries attempt+1).
    permanent_fail: case_ids that always fail with permanent=True.
    """

    def __init__(
        self,
        runtime_s: float = 0.0,
        fail_attempts: dict[str, int] | None = None,
        permanent_fail: set[str] | None = None,
    ):
        self.runtime_s = runtime_s
        self.fail_attempts = fail_attempts or {}
        self.permanent_fail = permanent_fail or set()

    def execute(
        self, payload: dict[str, Any], *, study_id: str, job_id: str, attempt: int
    ) -> ExecResult:
        case_id = payload["case_id"]
        if self.runtime_s:
            time.sleep(self.runtime_s)
        run_ref = f"run_{study_id[-6:]}_{case_id}_a{attempt}"
        if case_id in self.permanent_fail:
            return ExecResult(
                ok=False, run_ref=run_ref,
                error="solver diverged: infeasible design point (injected, permanent)",
                permanent=True,
            )
        if attempt <= self.fail_attempts.get(case_id, 0):
            return ExecResult(
                ok=False, run_ref=run_ref,
                error="solver diverged (injected, transient)",
            )
        overrides = payload.get("overrides", {})
        e_batt = float(overrides.get("battery.specific_energy_whkg", 500))
        range_nm = float(overrides.get("mission.range_nm", 500))
        # canned but deterministic physics-shaped numbers
        battery_mass = 400.0 * range_nm / e_batt
        result = {
            "mtow_kg": round(2500.0 + 1.5 * range_nm + battery_mass, 1),
            "fuel_burn_kg": round(0.25 * range_nm, 1),
            "battery_mass_kg": round(battery_mass, 1),
            "converged": True,
            "warm_start_run": payload.get("warm_start_run"),
        }
        return ExecResult(ok=True, run_ref=run_ref, result=result)


class FakeCheckSuite:
    """Stand-in for range-safety, run in-process on the worker (§8.5).
    Verdict levels injectable per case_id; defaults to pass."""

    def __init__(self, levels: dict[str, str] | None = None, default: str = "pass"):
        self.levels = levels or {}
        self.default = default

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
        level = self.levels.get(case_id, self.default)
        parity_ref = acceptance.get("parity", {}).get("reference", "n/a")
        checks = [
            {"check": "parity", "level": level, "detail": f"vs {parity_ref}"},
            {"check": "plausibility", "level": level,
             "detail": acceptance.get("plausibility_suite", "default_v0")},
        ]
        return level, checks
