"""Dotted-path --executor plugins: loader validation + CLI worker wiring."""

import pytest

from have_agent.cli import main as cli_main
from have_agent.plugins import PluginError, load_worker
from tests import plugin_fixture
from tests.conftest import force_job, job_row


class _Args:
    executor_opts: dict = {}


# --- loader unit tests -------------------------------------------------------

def test_load_worker_happy_path():
    executor, check_suite = load_worker("tests.plugin_fixture:make_worker", _Args())
    assert callable(executor.execute) and callable(check_suite.run)


def test_load_worker_accepts_none_check_suite():
    executor, check_suite = load_worker(
        "tests.plugin_fixture:make_worker_no_suite", _Args()
    )
    assert callable(executor.execute) and check_suite is None


@pytest.mark.parametrize("spec, fragment", [
    ("no-colon-here", "pkg.module:factory"),
    ("tests.plugin_fixture:", "pkg.module:factory"),
    (":make_worker", "pkg.module:factory"),
    ("tests.no_such_module:make_worker", "cannot import"),
    ("tests.plugin_fixture:missing", "no attribute"),
    ("tests.plugin_fixture:not_a_factory", "not callable"),
    ("tests.plugin_fixture:make_bare_executor", "must return (executor, check_suite)"),
    ("tests.plugin_fixture:make_non_executor", "lacks an execute() method"),
])
def test_load_worker_rejects_bad_specs(spec, fragment):
    with pytest.raises(PluginError) as exc_info:
        load_worker(spec, _Args())
    assert fragment in str(exc_info.value)


# --- CLI wiring --------------------------------------------------------------

def test_worker_run_with_plugin_executor(tmp_path, conn, study_id, capsys):
    db = tmp_path / "muroc.db"  # same file the conn fixture uses
    job_id = force_job(conn, study_id, state="queued")
    rc = cli_main([
        "--db", str(db), "worker", "run", "--id", "worker:plugin-1",
        "--executor", "tests.plugin_fixture:make_worker",
        "--executor-opt", "results_dir=/tmp/evals",
        "--executor-opt", "harness=opencode",
        "--poll", "0.01", "--idle-exit", "1",
    ])
    assert rc == 0
    assert "'succeeded': 1" in capsys.readouterr().out
    assert job_row(conn, job_id)["state"] not in ("queued", "assigned", "running")
    # the factory saw the parsed CLI namespace, opts already dict-ified
    assert plugin_fixture.LAST_ARGS.executor_opts == {
        "results_dir": "/tmp/evals", "harness": "opencode",
    }


def test_worker_run_rejects_bad_plugin_spec(tmp_path, capsys):
    db = tmp_path / "muroc.db"
    rc = cli_main([
        "--db", str(db), "worker", "run", "--id", "worker:plugin-bad",
        "--executor", "not-a-real-executor",
    ])
    assert rc == 1
    assert "pkg.module:factory" in capsys.readouterr().err
