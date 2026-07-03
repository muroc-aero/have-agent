"""End-to-end: submit the Brelje YAML, approve, run two fake workers with
3 injected failures; verify retries, terminal states, event-log completeness,
and that `have status` renders sanely."""

import json
import threading
from pathlib import Path

from have_agent import connect, migrate, study_transition
from have_agent.cli import main as cli_main
from have_agent.executor import FakeOCPExecutor
from have_agent.substrate import TERMINAL_JOB_STATES
from have_agent.worker import Worker
from tests.test_decompose import BRELJE_YAML

# 3 injected failures: two transient cases (recover via auto-retry) and one
# permanently infeasible design point (triage -> infeasible)
FAIL_TRANSIENT = {"e300_r1000": 1, "e400_r900": 1}
FAIL_PERMANENT = {"e800_r1000"}


def _worker(db, wid):
    return Worker(
        db, wid,
        FakeOCPExecutor(
            runtime_s=0.001,
            fail_attempts=FAIL_TRANSIENT,
            permanent_fail=FAIL_PERMANENT,
        ),
        poll_s=0.01,
    )


def test_e2e_brelje_replication(tmp_path, capsys):
    db = tmp_path / "muroc.db"
    conn = connect(db)
    migrate(conn)

    # submit (runs DECOMPOSE) + approve via the CLI surface
    request = tmp_path / "brelje.yaml"
    request.write_text(BRELJE_YAML)
    assert cli_main(["--db", str(db), "submit", str(request)]) == 0
    study_id = capsys.readouterr().out.split()[1]
    assert cli_main(["--db", str(db), "review", study_id]) == 0
    assert cli_main(["--db", str(db), "approve", study_id]) == 0
    capsys.readouterr()

    # two workers hammer the same DB until the study reaches review
    stop = threading.Event()
    workers = [_worker(db, "worker:alpha"), _worker(db, "worker:beta")]
    threads = [
        threading.Thread(target=w.run, kwargs={"stop": stop, "max_idle_polls": 400})
        for w in workers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    stop.set()
    assert not any(t.is_alive() for t in threads), "workers wedged"

    study = conn.execute("SELECT * FROM study WHERE id = ?", (study_id,)).fetchone()
    assert study["status"] == "review"

    # --- terminal states -----------------------------------------------------
    jobs = conn.execute("SELECT * FROM job WHERE study_id = ?", (study_id,)).fetchall()
    assert all(j["state"] in TERMINAL_JOB_STATES for j in jobs)

    by = lambda t, s: [j for j in jobs if j["type"] == t and j["state"] == s]  # noqa: E731
    # 48 cases + 2 transient retries = 50 ANALYSIS rows:
    # 47 accepted, 2 retry_spawned, 1 infeasible (permanent case)
    assert len(by("ANALYSIS", "accepted")) == 47
    assert len(by("ANALYSIS", "retry_spawned")) == 2
    assert len(by("ANALYSIS", "infeasible")) == 1
    # 48 CHECKs: 47 accepted, 1 cancelled (dead branch under the infeasible case)
    assert len(by("CHECK", "accepted")) == 47
    assert len(by("CHECK", "cancelled")) == 1
    # 3 TRIAGE jobs (one per injected failure), all accepted
    assert len(by("TRIAGE", "accepted")) == 3
    assert len(by("REPORT", "accepted")) == 1

    # --- retries -------------------------------------------------------------
    retries = [j for j in jobs if j["parent_job_id"] and j["type"] == "ANALYSIS"]
    assert {j["attempt"] for j in retries} == {2}
    assert all(j["state"] == "accepted" for j in retries)  # recovery rate 2/2
    failed_events = conn.execute(
        "SELECT COUNT(*) FROM event e JOIN job j ON j.id = e.object_id"
        " WHERE e.verb = 'job.failed' AND j.study_id = ?",
        (study_id,),
    ).fetchone()[0]
    assert failed_events == 3  # exactly the injected failures

    # --- report --------------------------------------------------------------
    assert study["conclusion_ref"] is not None
    briefing = Path(study["conclusion_ref"]).read_text()
    assert "Brelje" in briefing and "recovery_rate" in briefing
    assert conn.execute(
        "SELECT COUNT(*) FROM event WHERE verb = 'report.published'"
        " AND object_id = ?", (study_id,),
    ).fetchone()[0] == 1

    # --- event log completeness ------------------------------------------------
    # every job's event trail is a contiguous from/to chain starting at
    # 'proposed' and ending at its current state — no transition bypassed
    # the substrate, no event lost
    for j in jobs:
        chain = [
            json.loads(e["payload_json"])
            for e in conn.execute(
                "SELECT payload_json FROM event WHERE object_type = 'job'"
                " AND object_id = ? AND verb LIKE 'job.%' AND verb != 'job.proposed'"
                " ORDER BY id",
                (j["id"],),
            )
        ]
        assert chain, f"job {j['id']} has no transition events"
        assert chain[0]["from"] == "proposed"
        assert chain[-1]["to"] == j["state"]
        for prev, nxt in zip(chain, chain[1:], strict=False):
            assert prev["to"] == nxt["from"], f"gap in event chain for {j['id']}"
    study_events = [
        e["verb"]
        for e in conn.execute(
            "SELECT verb FROM event WHERE object_id = ? AND verb LIKE 'study.%'"
            " ORDER BY id",
            (study_id,),
        )
    ]
    assert study_events == [
        "study.submitted", "study.plan_proposed", "study.approved",
        "study.started", "study.review_ready",
    ]

    # --- `have status` renders sanely -----------------------------------------
    assert cli_main(["--db", str(db), "status", study_id]) == 0
    out = capsys.readouterr().out
    assert "[review]" in out
    assert "ANALYSIS" in out and "accepted=47" in out
    assert "worker:alpha" in out and "worker:beta" in out

    # human closes the study
    study_transition(conn, study_id, "closed", "human:alex")
    assert cli_main(["--db", str(db), "report", study_id]) == 0
    assert "## Metrics" in capsys.readouterr().out
    conn.close()


def test_e2e_abort_cascades(tmp_path, capsys):
    db = tmp_path / "muroc.db"
    conn = connect(db)
    migrate(conn)
    request = tmp_path / "brelje.yaml"
    request.write_text(BRELJE_YAML)
    cli_main(["--db", str(db), "submit", str(request)])
    study_id = capsys.readouterr().out.split()[1]
    cli_main(["--db", str(db), "approve", study_id])
    assert cli_main(["--db", str(db), "abort", study_id]) == 0
    assert conn.execute(
        "SELECT status FROM study WHERE id = ?", (study_id,)
    ).fetchone()[0] == "aborted"
    states = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT state FROM job WHERE study_id = ?", (study_id,)
        )
    }
    assert states == {"cancelled"}
    conn.close()


def test_worker_survives_handler_crash(tmp_path):
    """A crashing job lands in failed (not a dead worker loop)."""
    db = tmp_path / "muroc.db"
    conn = connect(db)
    migrate(conn)

    class ExplodingExecutor:
        def execute(self, payload, *, study_id, job_id, attempt):
            raise RuntimeError("boom")

    from tests.conftest import force_job, force_study

    sid = force_study(conn, status="running")
    job = force_job(conn, sid, state="queued", max_attempts=1)
    w = Worker(db, "worker:x", ExplodingExecutor(), run_ticks=False, poll_s=0.01)
    w.run(max_idle_polls=3, max_jobs=1)
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job,)).fetchone()
    assert row["state"] == "failed"
    ev = conn.execute(
        "SELECT payload_json FROM event WHERE object_id = ? AND verb = 'job.failed'",
        (job,),
    ).fetchone()
    assert "boom" in ev["payload_json"]
    conn.close()
