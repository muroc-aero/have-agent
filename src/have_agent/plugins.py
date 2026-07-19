"""Dotted-path worker plugins — the third --executor choice (DECISIONS #32).

``have worker run --executor pkg.module:factory`` imports ``pkg.module`` and
calls ``factory(args)`` with the parsed worker CLI namespace;
``args.executor_opts`` carries any ``--executor-opt KEY=VALUE`` pairs. The
factory returns ``(executor, check_suite)`` — any objects satisfying the
Executor / CheckSuite protocols in executor.py; check_suite may be None to
accept the worker's FakeCheckSuite default.

This keeps have-agent ignorant of what executes: a sibling repo (e.g.
hangar-evals) ships the factory, and the worker launch environment supplies
the import path (uv --with), exactly as the hangar executor already relies
on the-hangar being importable.
"""

from importlib import import_module
from typing import Any


class PluginError(Exception):
    """An --executor plugin spec that cannot be loaded or is malformed."""


def load_worker(spec: str, args: Any) -> tuple[Any, Any]:
    """Resolve ``pkg.module:factory``, call it, and validate the pair."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise PluginError(
            f"--executor {spec!r} is not 'fake', 'hangar', or a plugin spec"
            " of the form pkg.module:factory"
        )
    try:
        module = import_module(module_name)
    except ImportError as e:
        raise PluginError(
            f"--executor {spec!r}: cannot import {module_name!r}: {e}"
        ) from e
    try:
        factory = getattr(module, attr)
    except AttributeError as e:
        raise PluginError(
            f"--executor {spec!r}: module {module_name!r} has no attribute {attr!r}"
        ) from e
    if not callable(factory):
        raise PluginError(f"--executor {spec!r}: {attr!r} is not callable")
    pair = factory(args)
    try:
        executor, check_suite = pair
    except (TypeError, ValueError) as e:
        raise PluginError(
            f"--executor {spec!r}: factory must return (executor, check_suite),"
            f" got {type(pair).__name__}"
        ) from e
    if not callable(getattr(executor, "execute", None)):
        raise PluginError(f"--executor {spec!r}: executor lacks an execute() method")
    if check_suite is not None and not callable(getattr(check_suite, "run", None)):
        raise PluginError(f"--executor {spec!r}: check_suite lacks a run() method")
    return executor, check_suite
