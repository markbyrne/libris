"""Regression guard: `logging.*(..., extra={...})` calls must never use a
dict key that collides with a built-in LogRecord attribute (filename, name,
module, msg, args, levelname, ...) — stdlib logging raises
`KeyError: "Attempt to overwrite '<key>' in LogRecord"` the moment such a
call is actually emitted, which turns "add a log line" into a runtime crash
at whatever severity the call site chose (often error-path logging, so the
crash hides the original error entirely).

libris logs `extra={...}` at ~200 call sites across pipeline.py, calibre/*,
watcher/*, web/*, etc. This file has two tests:

  1. A static AST scan of every source file, asserting zero violations today
     (a real regression finder, not just a demonstration).
  2. A demonstration that the detector actually flags a bad call (so this
     test can't silently pass by failing to find anything), and that stdlib
     logging really does crash on such a call.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import libris

# The reserved LogRecord attribute names — taken from a live LogRecord's
# __dict__ rather than hard-coded, so this stays correct if a future Python
# version adds new attributes.
_RESERVED_KEYS = frozenset(
    logging.LogRecord("_probe", logging.INFO, __file__, 0, "", None, None).__dict__.keys()
)

_LOG_METHOD_NAMES = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _find_reserved_extra_key_violations(source: str, filename: str) -> list[str]:
    """Return human-readable violation strings for any `extra={...}` keyword
    argument (on a call that looks like a logging call) whose dict literal
    contains a key colliding with a reserved LogRecord attribute.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        method_name = getattr(func, "attr", None) or getattr(func, "id", None)
        if method_name not in _LOG_METHOD_NAMES:
            continue
        for kw in node.keywords:
            if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                continue
            for key_node in kw.value.keys:
                is_str_key = isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
                if is_str_key and key_node.value in _RESERVED_KEYS:
                    violations.append(
                        f"{filename}:{node.lineno}: extra key {key_node.value!r} "
                        f"collides with a reserved LogRecord attribute"
                    )
    return violations


class TestNoReservedKeysInSource:
    def test_no_call_site_uses_a_reserved_extra_key(self):
        package_root = Path(libris.__file__).parent
        all_violations: list[str] = []
        for path in package_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            all_violations.extend(_find_reserved_extra_key_violations(source, str(path)))
        assert not all_violations, "\n" + "\n".join(all_violations)


class TestDetectorActuallyDetects:
    """Prove the AST scan isn't vacuously passing by feeding it known-bad
    snippets, and prove the underlying danger is real (stdlib logging
    genuinely raises on these calls)."""

    @pytest.mark.parametrize("bad_key", ["filename", "name", "module", "msg", "args", "lineno"])
    def test_flags_each_reserved_key(self, bad_key):
        source = f'log.info("msg", extra={{"{bad_key}": "x"}})\n'
        violations = _find_reserved_extra_key_violations(source, "<test>")
        assert len(violations) == 1
        assert bad_key in violations[0]

    def test_ignores_non_reserved_keys(self):
        source = 'log.info("msg", extra={"book_id": 1, "file": "x"})\n'
        assert _find_reserved_extra_key_violations(source, "<test>") == []

    def test_ignores_non_logging_calls(self):
        source = 'some_func("msg", extra={"filename": "x"})\n'
        assert _find_reserved_extra_key_violations(source, "<test>") == []

    def test_stdlib_logging_really_does_crash_on_reserved_key(self, caplog):
        """The actual danger this guard prevents: unlike a normal Python
        crash, this one only fires when the log line executes AND a handler
        actually formats/emits the record — so it can lurk for a long time
        in an error-handling branch before someone hits it in production.
        """
        logger = logging.getLogger("libris.tests.reserved_key_probe")
        with pytest.raises(KeyError):
            logger.handle(
                logger.makeRecord(
                    logger.name, logging.INFO, __file__, 0, "msg", None, None,
                    extra={"filename": "boom.epub"},
                )
            )
