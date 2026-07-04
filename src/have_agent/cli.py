"""`have` — CLI surface v0 (§7). argparse, terminal output only."""

import argparse
import contextlib
import getpass
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from have_agent.db import connect, migrate
from have_agent.decompose import submit_study
from have_agent.executor import FakeCheckSuite, FakeOCPExecutor
from have_agent.substrate import SubstrateError, study_transition, transition
from have_agent.worker import Worker


def _human() -> str:
    return f"human:{getpass.getuser()}"


def _open(args) -> sqlite3.Connection:
    conn = connect(args.db)
    migrate(conn)
    return conn


def _find(conn, object_id: str) -> tuple[str, sqlite3.Row] | None:
    for kind in ("study", "job"):
        row = conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (object_id,)).fetchone()  # noqa: S608
        if row:
            return kind, row
    return None


def cmd_submit(conn, args) -> int:
    yaml_text = Path(args.request).read_text()
    study_id, plan = submit_study(conn, yaml_text, _human())
    counts = plan["job_counts"]
    print(f"study {study_id} proposed: {len(plan['cases'])} cases,"
          f" {sum(counts.values())} jobs {counts}")
    print(f"next: have approve {study_id}")
    return 0


def cmd_review(conn, args) -> int:
    found = _find(conn, args.study_id)
    if not found or found[0] != "study":
        print(f"no study {args.study_id}", file=sys.stderr)
        return 1
    study = found[1]
    print(f"{study['id']}  {study['title']}  [{study['status']}]  owner={study['owner']}")
    if study["plan_proposal_json"]:
        plan = json.loads(study["plan_proposal_json"])
        print(f"plan: {plan['job_counts']}  order: {plan.get('case_order')}"
              f"  retry: {plan.get('retry_strategy')}")
        cases = plan.get("cases", [])
        preview = ", ".join(c["case_id"] for c in cases[:8])
        print(f"cases ({len(cases)}): {preview}{', ...' if len(cases) > 8 else ''}")
    inbox = conn.execute(
        "SELECT j.id, j.type, j.state, v.level, v.summary FROM job j"
        " LEFT JOIN verdict v ON v.id = j.verdict_id"
        " WHERE j.study_id = ? AND j.state IN ('review', 'proposed', 'escalated')"
        " ORDER BY j.state, j.created_at",
        (study["id"],),
    ).fetchall()
    if inbox and study["status"] != "proposed":
        print("review inbox:")
        for j in inbox:
            extra = f"  verdict={j['level']} {j['summary'] or ''}" if j["level"] else ""
            print(f"  {j['id']}  {j['type']:<8} [{j['state']}]{extra}")
    return 0


def cmd_approve(conn, args) -> int:
    found = _find(conn, args.object_id)
    if not found:
        print(f"no study or job {args.object_id}", file=sys.stderr)
        return 1
    kind, row = found
    actor = _human()
    if kind == "study":
        if row["status"] == "review":  # §7 has no close command; approve closes
            study_transition(conn, row["id"], "closed", actor, expected_status="review")
            print(f"study {row['id']} closed")
            return 0
        study_transition(conn, row["id"], "approved", actor, expected_status="proposed")
        n = 0
        for j in conn.execute(
            "SELECT id FROM job WHERE study_id = ? AND state = 'proposed'", (row["id"],)
        ).fetchall():
            transition(conn, j["id"], "approved", actor, expected_state="proposed")
            n += 1
        print(f"study {row['id']} approved ({n} jobs)")
    else:
        to = "approved" if row["state"] == "proposed" else "accepted"
        transition(conn, row["id"], to, actor)
        print(f"job {row['id']} {to}")
    return 0


def cmd_reject(conn, args) -> int:
    found = _find(conn, args.job_id)
    if not found or found[0] != "job":
        print(f"no job {args.job_id}", file=sys.stderr)
        return 1
    row = found[1]
    payload = {"reason": args.reason} if args.reason else None
    to = "cancelled" if row["state"] == "proposed" else "rejected"
    transition(conn, row["id"], to, _human(), payload)
    print(f"job {row['id']} {to}")
    return 0


def cmd_status(conn, args) -> int:
    where, params = ("WHERE id = ?", (args.study_id,)) if args.study_id else ("", ())
    studies = conn.execute(
        f"SELECT * FROM study {where} ORDER BY created_at", params  # noqa: S608
    ).fetchall()
    if args.study_id and not studies:
        print(f"no study {args.study_id}", file=sys.stderr)
        return 1
    for s in studies:
        print(f"{s['id']}  {s['title']}  [{s['status']}]")
        counts = conn.execute(
            "SELECT type, state, COUNT(*) AS n FROM job WHERE study_id = ?"
            " GROUP BY type, state ORDER BY type, state",
            (s["id"],),
        ).fetchall()
        by_type: dict[str, list[str]] = {}
        for c in counts:
            by_type.setdefault(c["type"], []).append(f"{c['state']}={c['n']}")
        for t, parts in by_type.items():
            print(f"  {t:<8} {'  '.join(parts)}")
        if s["conclusion_ref"]:
            print(f"  report: {s['conclusion_ref']}")
    workers = conn.execute("SELECT * FROM worker ORDER BY id").fetchall()
    if workers:
        print("workers:")
        for w in workers:
            busy = conn.execute(
                "SELECT COUNT(*) FROM job WHERE assigned_worker = ?"
                " AND state IN ('assigned', 'running')",
                (w["id"],),
            ).fetchone()[0]
            print(f"  {w['id']}  [{w['status']}]  {busy}/{w['capacity']} busy"
                  f"  hb={w['last_heartbeat']}")
    return 0


def cmd_events(conn, args) -> int:
    where, params = [], []
    if args.object:
        where.append("object_id = ?")
        params.append(args.object)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    last_id = ""
    while True:
        rows = conn.execute(
            f"SELECT * FROM event {clause}{' AND' if where else ' WHERE'} id > ?"  # noqa: S608
            " ORDER BY id LIMIT ?",
            (*params, last_id, args.limit),
        ).fetchall()
        for e in rows:
            payload = e["payload_json"] if e["payload_json"] not in (None, "{}") else ""
            print(f"{e['ts']}  {e['actor']:<20} {e['verb']:<22}"
                  f" {e['object_type']}:{e['object_id']}  {payload}")
            last_id = e["id"]
        if not args.follow:
            return 0
        time.sleep(0.5)


def cmd_worker_run(conn, args) -> int:
    conn.close()  # the worker owns its own connection
    if args.executor == "hangar":
        from have_agent.checks import RangeSafetyCheckSuite
        from have_agent.hangar_executor import HangarExecutor

        param_map: dict = {}
        for spec in args.param_map or ():
            key, _, path = spec.partition("=")
            existing = param_map.get(key)
            if existing is None:
                param_map[key] = path
            elif isinstance(existing, list):
                existing.append(path)
            else:
                param_map[key] = [existing, path]
        # unified DB (spec §8.3): the-hangar's PROV tables live in the same
        # muroc.db as the substrate unless --omd-db points elsewhere
        omd_db = args.omd_db or args.db
        executor = HangarExecutor(
            plan_root=args.plan_root,
            param_map=param_map,
            mode=args.mode,
            timeout_s=args.timeout,
            omd_db_path=omd_db,
        )
        check_suite = RangeSafetyCheckSuite(
            reference_root=args.reference_root, db_path=omd_db,
        )
    else:
        fail_attempts = {}
        for spec in args.fail_case or ():
            case, _, n = spec.partition(":")
            fail_attempts[case] = int(n or 1)
        check_levels = {}
        for spec in args.check_level or ():
            case, _, level = spec.partition(":")
            check_levels[case] = level or "warn"
        executor = FakeOCPExecutor(
            runtime_s=args.runtime,
            fail_attempts=fail_attempts,
            permanent_fail=set(args.permanent_fail or ()),
        )
        check_suite = FakeCheckSuite(levels=check_levels)
    worker = Worker(
        args.db,
        args.id,
        executor,
        check_suite=check_suite,
        capabilities={"solvers": args.solvers.split(","), "mem_mb": args.mem},
        capacity=args.capacity,
        poll_s=args.poll,
    )
    stats = worker.run(max_idle_polls=args.idle_exit)
    print(f"{args.id} done: {stats}")
    return 0


def cmd_abort(conn, args) -> int:
    found = _find(conn, args.study_id)
    if not found or found[0] != "study":
        print(f"no study {args.study_id}", file=sys.stderr)
        return 1
    actor = _human()
    study_transition(conn, args.study_id, "aborted", actor)
    n = 0
    for j in conn.execute(
        "SELECT id, state FROM job WHERE study_id = ?"
        " AND state IN ('proposed', 'approved', 'queued', 'assigned')",
        (args.study_id,),
    ).fetchall():
        transition(conn, j["id"], "cancelled", actor, {"reason": "study_aborted"},
                   expected_state=j["state"])
        n += 1
    print(f"study {args.study_id} aborted ({n} jobs cancelled;"
          " running jobs finish or lease out)")
    return 0


def cmd_report(conn, args) -> int:
    row = conn.execute(
        "SELECT conclusion_ref FROM study WHERE id = ?", (args.study_id,)
    ).fetchone()
    if not row or not row["conclusion_ref"]:
        print(f"no report published for {args.study_id}", file=sys.stderr)
        return 1
    print(Path(row["conclusion_ref"]).read_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="have", description="have-agent control plane")
    p.add_argument("--db", default=os.environ.get("MUROC_DB", "muroc.db"))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("submit", help="submit a StudyRequest YAML (runs DECOMPOSE)")
    s.add_argument("request")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("review", help="render plan proposal / review inbox")
    s.add_argument("study_id")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("approve", help="approve a study (and its jobs) or a job")
    s.add_argument("object_id")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reject", help="reject a job in review (or cancel a proposed one)")
    s.add_argument("job_id")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("status", help="queue + study views")
    s.add_argument("study_id", nargs="?")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("events", help="tail the COP")
    s.add_argument("--follow", action="store_true")
    s.add_argument("--object")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_events)

    w = sub.add_parser("worker", help="worker commands")
    wsub = w.add_subparsers(dest="worker_command", required=True)
    s = wsub.add_parser("run", help="start a pull worker loop")
    s.add_argument("--id", required=True, help="e.g. worker:vps-1")
    s.add_argument("--executor", choices=("fake", "hangar"), default="fake",
                   help="hangar = real the-hangar run_plan + range-safety checks")
    s.add_argument("--capacity", type=int, default=1)
    s.add_argument("--solvers", default="ocp")
    s.add_argument("--mem", type=int, default=8192)
    s.add_argument("--poll", type=float, default=0.5)
    s.add_argument("--idle-exit", type=int, default=None,
                   help="exit after N idle polls (default: run forever)")
    # hangar executor options
    s.add_argument("--plan-root", help="directory resolving relative plan_refs")
    s.add_argument("--param-map", action="append", metavar="SWEEP_KEY=PLAN_PATH",
                   help="override translation, e.g. mission.range_nm=design_range.value")
    s.add_argument("--reference-root", help="directory resolving acceptance parity refs"
                   " (default: cwd)")
    s.add_argument("--omd-db", help="the-hangar analysis DB (default: --db, unified)")
    s.add_argument("--mode", choices=("analysis", "optimize"), default="optimize")
    s.add_argument("--timeout", type=int, default=None, help="per-run wallclock seconds")
    # fake executor options
    s.add_argument("--runtime", type=float, default=1.0, help="fake seconds per case")
    s.add_argument("--fail-case", action="append", metavar="CASE[:N]",
                   help="inject failure: case fails while attempt <= N")
    s.add_argument("--permanent-fail", action="append", metavar="CASE")
    s.add_argument("--check-level", action="append", metavar="CASE[:LEVEL]")
    s.set_defaults(fn=cmd_worker_run)

    s = sub.add_parser("abort", help="abort a study, cancel its pre-running jobs")
    s.add_argument("study_id")
    s.set_defaults(fn=cmd_abort)

    s = sub.add_parser("report", help="print the study briefing artifact")
    s.add_argument("study_id")
    s.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = _open(args)
    try:
        return args.fn(conn, args)
    except SubstrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(sqlite3.ProgrammingError):
            conn.close()  # worker run closes it early


if __name__ == "__main__":
    raise SystemExit(main())
