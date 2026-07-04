"""Pull-based worker loop: register, claim, execute, emit events.

Each Worker.run() opens its own connection (thread-safe: one worker per
thread/process, one shared muroc.db). By default the worker also runs the
control tick each poll, so two workers + the DB are a complete deployment —
no separate scheduler daemon needed for v0.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from have_agent.control import control_tick
from have_agent.db import connect
from have_agent.executor import CheckSuite, Executor, FakeCheckSuite
from have_agent.report import build_report
from have_agent.scheduler import claim_next, heartbeat, lease_expiry
from have_agent.substrate import record_verdict, register_worker, transition
from have_agent.triage import run_triage


class Worker:
    def __init__(
        self,
        db_path: str | Path,
        worker_id: str,
        executor: Executor,
        *,
        check_suite: CheckSuite | None = None,
        capabilities: dict[str, Any] | None = None,
        capacity: int = 1,
        poll_s: float = 0.05,
        run_ticks: bool = True,
        artifacts_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.worker_id = worker_id
        self.executor = executor
        self.check_suite = check_suite or FakeCheckSuite()
        self.capabilities = capabilities or {"solvers": ["ocp"], "mem_mb": 8192}
        self.capacity = capacity
        self.poll_s = poll_s
        self.run_ticks = run_ticks
        self.artifacts_dir = (
            Path(artifacts_dir) if artifacts_dir else self.db_path.parent / "artifacts"
        )
        self.stats = {"claimed": 0, "succeeded": 0, "failed": 0}

    def run(
        self,
        *,
        stop: threading.Event | None = None,
        max_idle_polls: int | None = None,
        max_jobs: int | None = None,
    ) -> dict[str, int]:
        conn = connect(self.db_path)
        try:
            register_worker(conn, self.worker_id, self.capabilities, capacity=self.capacity)
            idle = 0
            while not (stop and stop.is_set()):
                if max_jobs is not None and self.stats["claimed"] >= max_jobs:
                    break
                if self.run_ticks:
                    control_tick(conn)
                heartbeat(conn, self.worker_id)
                job = claim_next(conn, self.worker_id)
                if job is None:
                    idle += 1
                    if max_idle_polls is not None and idle >= max_idle_polls:
                        break
                    time.sleep(self.poll_s)
                    continue
                idle = 0
                self.stats["claimed"] += 1
                self._process(conn, job)
            return self.stats
        finally:
            conn.close()

    def _process(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        resource = json.loads(job["resource_json"])
        transition(
            conn, job["id"], "running", self.worker_id,
            expected_state="assigned",
            lease_expires_at=lease_expiry(resource),
        )
        try:
            handler = {
                "ANALYSIS": self._run_analysis,
                "CHECK": self._run_check,
                "TRIAGE": self._run_triage,
                "REPORT": self._run_report,
            }.get(job["type"])
            if handler is None:
                raise NotImplementedError(f"job type {job['type']} not implemented in v0")
            ok = handler(conn, job)
            self.stats["succeeded" if ok else "failed"] += 1
        except Exception as exc:  # noqa: BLE001 — a crashed handler must not kill the loop
            self.stats["failed"] += 1
            transition(
                conn, job["id"], "failed", self.worker_id,
                {"error": f"{type(exc).__name__}: {exc}", "permanent": False},
                expected_state="running",
            )

    def _run_analysis(self, conn: sqlite3.Connection, job: sqlite3.Row) -> bool:
        payload = json.loads(job["payload_json"])
        res = self.executor.execute(
            payload, study_id=job["study_id"], job_id=job["id"], attempt=job["attempt"]
        )
        if res.ok:
            transition(
                conn, job["id"], "succeeded", self.worker_id,
                {"result": res.result},
                expected_state="running",
                run_ref=res.run_ref, artifact_refs=res.artifacts,
            )
        else:
            transition(
                conn, job["id"], "failed", self.worker_id,
                {"error": res.error, "permanent": res.permanent},
                expected_state="running",
                run_ref=res.run_ref,
            )
        return res.ok

    def _run_check(self, conn: sqlite3.Connection, job: sqlite3.Row) -> bool:
        payload = json.loads(job["payload_json"])
        upstream = conn.execute(
            "SELECT u.run_ref, u.payload_json FROM job_dep d JOIN job u ON u.id = d.depends_on"
            " WHERE d.job_id = ? LIMIT 1",
            (job["id"],),
        ).fetchone()
        run_ref = upstream["run_ref"] if upstream else None
        upstream_payload = json.loads(upstream["payload_json"]) if upstream else {}
        case_id = upstream_payload.get("case_id", "?")
        level, checks = self.check_suite.run(
            case_id, run_ref, payload.get("acceptance", {}),
            overrides=upstream_payload.get("overrides") or {},
        )
        verdict_id = record_verdict(
            conn, job["id"], level, checks, self.worker_id,
            run_ref=run_ref, summary=f"{payload.get('check_suite')}: {level} ({case_id})",
        )
        transition(
            conn, job["id"], "succeeded", self.worker_id,
            {"level": level, "case_id": case_id},
            expected_state="running",
            run_ref=run_ref, verdict_id=verdict_id,
        )
        return True

    def _run_triage(self, conn: sqlite3.Connection, job: sqlite3.Row) -> bool:
        result = run_triage(conn, job)
        transition(
            conn, job["id"], "succeeded", self.worker_id, result,
            expected_state="running",
        )
        return True

    def _run_report(self, conn: sqlite3.Connection, job: sqlite3.Row) -> bool:
        path = build_report(conn, job, self.artifacts_dir)
        transition(
            conn, job["id"], "succeeded", self.worker_id,
            {"artifact": str(path)},
            expected_state="running",
            artifact_refs=[str(path)],
        )
        return True
