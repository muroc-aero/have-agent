"""Real executor: dispatch ANALYSIS jobs to the-hangar's omd run_plan.

Drops in behind the Executor protocol where FakeOCPExecutor sits today.
run_plan carries the §8.2 interface — (plan_ref, overrides, case_id,
warm_start_run), idempotent on (study_id, case_id, attempt) — so a worker
that dies mid-job and gets its attempt requeued replays the recorded
result instead of re-running the solve.

the-hangar is imported lazily at execute() time: have-agent itself has no
dependency on it, and tests stub the module (see tests/test_hangar_executor).

The StudyRequest sweep keys are domain parameter names (e.g.
``battery.specific_energy_whkg``), not omd plan paths; ``param_map``
translates them (DECISIONS.md #16). Unmapped keys are passed through
unchanged, assumed to already be plan-path expressions.
"""

from pathlib import Path
from typing import Any

from have_agent.executor import ExecResult

OK_STATUSES = frozenset({"completed", "converged"})
# No run_id means the plan never materialized (schema/override errors):
# identical inputs can never succeed, so retrying is pointless.
_PERMANENT_ERROR_PATHS = frozenset({"overrides", "materialize", "plan", "(root)"})


class HangarExecutor:
    """Executor that runs each case through hangar.omd.run.run_plan.

    plan_root: directory against which a relative payload plan_ref is
        resolved (e.g. the deployment's plan store checkout).
    param_map: sweep-key -> plan-path translation for overrides. A value
        may be a list when one domain parameter fans out to several plan
        paths (the Brelje King Air plan binds battery specific energy to
        both mission_params and propulsion_overrides).
    mode / recording_level / timeout_s / omd_db_path: passed to run_plan.
    """

    def __init__(
        self,
        plan_root: str | Path | None = None,
        param_map: dict[str, str | list[str]] | None = None,
        *,
        mode: str = "analysis",
        recording_level: str = "driver",
        timeout_s: int | None = None,
        omd_db_path: str | Path | None = None,
    ):
        self.plan_root = Path(plan_root) if plan_root else None
        self.param_map = param_map or {}
        self.mode = mode
        self.recording_level = recording_level
        self.timeout_s = timeout_s
        self.omd_db_path = omd_db_path

    def _resolve_plan(self, plan_ref: str) -> Path:
        path = Path(plan_ref)
        if not path.is_absolute() and self.plan_root:
            path = self.plan_root / path
        return path

    def _translate(self, overrides: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in overrides.items():
            paths = self.param_map.get(k, k)
            for path in [paths] if isinstance(paths, str) else paths:
                out[path] = v
        return out

    def execute(
        self, payload: dict[str, Any], *, study_id: str, job_id: str, attempt: int
    ) -> ExecResult:
        from hangar.omd.run import run_plan  # requires the-hangar installed

        result = run_plan(
            self._resolve_plan(payload["plan_ref"]),
            mode=self.mode,
            recording_level=self.recording_level,
            db_path=self.omd_db_path,
            timeout_seconds=self.timeout_s,
            overrides=self._translate(payload.get("overrides") or {}),
            case_id=payload["case_id"],
            study_id=study_id,
            attempt=attempt,
            warm_start_run=payload.get("warm_start_run"),
        )
        ok = result.get("status") in OK_STATUSES
        errors = result.get("errors") or []
        error = (
            "; ".join(f"{e.get('path')}: {e.get('message')}" for e in errors)[:500]
            if errors else None
        )
        permanent = not ok and (
            result.get("run_id") is None
            or any(e.get("path") in _PERMANENT_ERROR_PATHS for e in errors)
        )
        return ExecResult(
            ok=ok,
            run_ref=result.get("run_id"),
            result=result.get("summary") or None,
            error=error,
            permanent=permanent,
        )
