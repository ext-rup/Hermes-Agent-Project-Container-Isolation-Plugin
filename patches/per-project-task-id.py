#!/usr/bin/env python3
"""Patch Hermes' _resolve_container_task_id() to key containers by project.

Why this is needed
------------------
Hermes collapses every chat session's task_id to "default"
(tools/terminal_tool.py, _resolve_container_task_id). ``_active_environments``
is keyed on that value, so one environment object is built per gateway process
and reused for every chat in every project — and because the cached object is
reused directly, Hermes never shells out to `docker` again. That happens above
the docker layer, so the shim cannot influence it: there is no invocation to
intercept.

Scoping the returned id by active project makes ``_active_environments`` key
per project, so each project chat builds its own environment. The shim's label
scoping and mount rewriting then line up with it automatically (its
scope_task_id() is idempotent, so an already-scoped id passes through
unchanged).

The RL/benchmark isolation-key branch is left exactly as-is.

Idempotent: re-running detects an applied patch and does nothing. Safe to run
after every `hermes update`.
"""

import os
import re
import shutil
import sys
import time

TARGET = os.environ.get("HERMES_TERMINAL_TOOL") or os.path.expanduser(
    "~/.hermes/hermes-agent/tools/terminal_tool.py"
)
MARKER = "# --- hermes-containers: per-project container scoping ---"

ANCHOR = """    if task_id and task_id in _task_env_overrides:
        overrides = _task_env_overrides[task_id]
        if set(overrides.keys()) & _ISOLATION_KEYS:
            return task_id
    return "default"
"""

REPLACEMENT = '''    if task_id and task_id in _task_env_overrides:
        overrides = _task_env_overrides[task_id]
        if set(overrides.keys()) & _ISOLATION_KEYS:
            return task_id

''' + "    " + MARKER + '''
    # Partition the container cache by Hermes project so each project chat
    # gets its own environment (and therefore its own container + workspace)
    # instead of sharing one "default" env per gateway process.
    # Reads ~/.hermes/projects.db active_id — the same store the desktop and
    # the project_switch tool write to. Any failure falls through to the
    # original "default" behaviour.
    try:
        import sqlite3 as _sqlite3

        _db = os.path.expanduser(
            os.path.join(os.environ.get("HERMES_HOME", "~/.hermes"), "projects.db")
        )
        if os.path.exists(_db):
            # projects.db is WAL. A mode=ro open needs the -shm sidecar and
            # fails if it has been checkpointed away, so fall back to a normal
            # connection (as Hermes' own projects_db.connect does). Never use
            # immutable=1 here: it bypasses the WAL and reads a stale active_id.
            try:
                _conn = _sqlite3.connect(
                    "file:%s?mode=ro" % _db, uri=True, timeout=2.0
                )
            except _sqlite3.Error:
                _conn = _sqlite3.connect(_db, timeout=2.0)
            try:
                _row = _conn.execute(
                    "SELECT p.slug FROM project_meta m "
                    "JOIN projects p ON p.id = m.value WHERE m.key = 'active_id'"
                ).fetchone()
            finally:
                _conn.close()
            if _row and _row[0]:
                _slug = re.sub(r"[^A-Za-z0-9_.-]", "_", str(_row[0]))[:40]
                if _slug:
                    return "default.%s" % _slug
    except Exception:
        pass
    # --- end hermes-containers ---
    return "default"
'''


def revert():
    """Undo the patch, restoring the original block.

    Needed when switching to the plugin: with both active the plugin's wrapper
    would see a non-"default" return value, treat it as an isolation-keyed
    task, and skip its own (per-session, concurrency-safe) scoping.
    """
    with open(TARGET) as f:
        source = f.read()
    if MARKER not in source:
        print("not applied — nothing to revert")
        return 0
    if REPLACEMENT not in source:
        print("error: patched block does not match; revert by hand or restore "
              "a .pre-per-project.* backup", file=sys.stderr)
        return 1
    backup = "%s.pre-revert.%s" % (TARGET, time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(TARGET, backup)
    with open(TARGET, "w") as f:
        f.write(source.replace(REPLACEMENT, ANCHOR))

    import py_compile

    try:
        py_compile.compile(TARGET, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print("error: reverted file failed to compile, restored:\n%s" % e,
              file=sys.stderr)
        return 1
    print("reverted %s (backup: %s)" % (TARGET, backup))
    return 0


def main():
    if not os.path.exists(TARGET):
        print("error: %s not found" % TARGET, file=sys.stderr)
        return 1

    if "--revert" in sys.argv:
        return revert()

    with open(TARGET) as f:
        source = f.read()

    if MARKER in source:
        print("already applied — nothing to do")
        return 0

    if ANCHOR not in source:
        print("error: anchor not found in %s" % TARGET, file=sys.stderr)
        print("       Hermes changed upstream; the patch needs updating.", file=sys.stderr)
        return 1

    if source.count(ANCHOR) != 1:
        print("error: anchor matched %d times; refusing to guess"
              % source.count(ANCHOR), file=sys.stderr)
        return 1

    # `re` and `os` are already imported at module scope in terminal_tool.py;
    # verify rather than assume, since the patch body uses both.
    for module in ("import re", "import os"):
        if not re.search(r"^%s$" % module, source, re.M):
            print("error: terminal_tool.py lacks '%s'" % module, file=sys.stderr)
            return 1

    backup = "%s.pre-per-project.%s" % (TARGET, time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(TARGET, backup)
    print("backed up -> %s" % backup)

    with open(TARGET, "w") as f:
        f.write(source.replace(ANCHOR, REPLACEMENT))

    # Syntax-check what we just wrote; restore on failure rather than leaving
    # a broken terminal_tool.py behind.
    import py_compile

    try:
        py_compile.compile(TARGET, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print("error: patched file failed to compile, restored backup:\n%s" % e,
              file=sys.stderr)
        return 1

    print("applied to %s" % TARGET)
    print("restart the gateway for it to take effect:")
    print("  launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
