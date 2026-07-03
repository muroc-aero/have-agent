"""CLI smoke tests for the commands the e2e flow doesn't exercise."""

from have_agent import connect, migrate
from have_agent.cli import main as cli_main
from tests.conftest import force_job, force_study, job_row


def _db(tmp_path):
    db = tmp_path / "muroc.db"
    conn = connect(db)
    migrate(conn)
    return db, conn


def test_reject_review_job(tmp_path, capsys):
    db, conn = _db(tmp_path)
    sid = force_study(conn, status="running")
    job = force_job(conn, sid, state="review")
    assert cli_main(["--db", str(db), "reject", job, "--reason", "parity off"]) == 0
    assert job_row(conn, job)["state"] == "rejected"


def test_reject_proposed_job_cancels(tmp_path, capsys):
    db, conn = _db(tmp_path)
    sid = force_study(conn, status="running")
    job = force_job(conn, sid, state="proposed")
    assert cli_main(["--db", str(db), "reject", job]) == 0
    assert job_row(conn, job)["state"] == "cancelled"


def test_approve_single_job(tmp_path, capsys):
    db, conn = _db(tmp_path)
    sid = force_study(conn, status="running")
    proposed = force_job(conn, sid, state="proposed")
    in_review = force_job(conn, sid, state="review")
    assert cli_main(["--db", str(db), "approve", proposed]) == 0
    assert job_row(conn, proposed)["state"] == "approved"
    assert cli_main(["--db", str(db), "approve", in_review]) == 0
    assert job_row(conn, in_review)["state"] == "accepted"


def test_events_renders(tmp_path, capsys):
    db, conn = _db(tmp_path)
    sid = force_study(conn, status="draft")
    job = force_job(conn, sid, state="proposed")
    cli_main(["--db", str(db), "approve", job])
    capsys.readouterr()
    assert cli_main(["--db", str(db), "events", "--object", job]) == 0
    out = capsys.readouterr().out
    assert "job.approved" in out and job in out


def test_approve_closes_study_in_review(tmp_path, capsys):
    from tests.conftest import study_row

    db, conn = _db(tmp_path)
    sid = force_study(conn, status="review")
    assert cli_main(["--db", str(db), "approve", sid]) == 0
    assert study_row(conn, sid)["status"] == "closed"


def test_unknown_ids_fail_cleanly(tmp_path, capsys):
    db, _ = _db(tmp_path)
    assert cli_main(["--db", str(db), "approve", "nope"]) == 1
    assert cli_main(["--db", str(db), "status", "nope"]) == 1
    assert cli_main(["--db", str(db), "report", "nope"]) == 1
