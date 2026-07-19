"""Importable plugin factories for test_plugins.py.

Lives as a real module (not a tmp file) because --executor plugin specs are
resolved with importlib against the worker environment — the same way a
sibling repo's factory would be.
"""

from have_agent.executor import FakeCheckSuite, FakeOCPExecutor

LAST_ARGS = None  # the namespace the factory saw, for assertions


def make_worker(args):
    global LAST_ARGS
    LAST_ARGS = args
    return FakeOCPExecutor(runtime_s=0.0), FakeCheckSuite()


def make_worker_no_suite(args):
    return FakeOCPExecutor(runtime_s=0.0), None


def make_bare_executor(args):
    # wrong shape: not an (executor, check_suite) pair
    return FakeOCPExecutor(runtime_s=0.0)


def make_non_executor(args):
    # right shape, but the executor half has no execute()
    return object(), None


not_a_factory = "just a string"
