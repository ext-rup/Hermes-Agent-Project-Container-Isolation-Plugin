"""hermes-projects-apple — per-project containers on Apple Container.

Two jobs:

1. Scope Hermes' container cache by project, so each project chat gets its own
   container with its own project directory at /workspace. See
   ``project_scope`` for why this cannot be done at the docker-CLI level.

2. Point Hermes at the Apple Container shim (``HERMES_DOCKER_BINARY``) unless
   it is already set, since Apple's ``container`` CLI is not Docker-compatible.

Enable exactly one of hermes-projects-apple / hermes-projects-docker: they wrap
the same function, and running Apple's shim against real Docker (or vice versa)
will not work.
"""

from __future__ import annotations

import logging
import os

from . import project_scope

logger = logging.getLogger(__name__)

# The shim ships inside this plugin, so installing the plugin is all that is
# needed — no separate install step and no ~/.hermes/.env edit. A shim placed
# at ~/.hermes/docker-wrapper (by install.sh, or by hand) is still honoured as
# a fallback.
BUNDLED_SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker-wrapper")
LEGACY_SHIM = os.path.expanduser("~/.hermes/docker-wrapper")


def _usable_shim() -> str:
    """Path to a runnable shim, preferring the bundled one. "" if none."""
    for path in (BUNDLED_SHIM, LEGACY_SHIM):
        if not os.path.exists(path):
            continue
        if not os.access(path, os.X_OK):
            # A shim copied by hand (or unpacked from an archive) can lose its
            # executable bit, and find_docker() skips anything not executable —
            # so it would be silently ignored. Fix it rather than warn.
            try:
                os.chmod(path, os.stat(path).st_mode | 0o111)
                logger.info("made shim executable: %s", path)
            except OSError as e:
                logger.warning("shim %s is not executable and chmod failed: %s", path, e)
                continue
        return path
    return ""


def _ensure_shim() -> None:
    """Point HERMES_DOCKER_BINARY at the shim unless the user set it."""
    current = (os.environ.get("HERMES_DOCKER_BINARY") or "").strip()
    if current:
        # An explicit setting always wins — it may be a deliberate override.
        if not os.path.exists(current):
            logger.warning(
                "HERMES_DOCKER_BINARY=%s does not exist; Hermes will fall back "
                "to PATH and probably find a Docker CLI that cannot drive "
                "Apple Container.", current,
            )
        return

    shim = _usable_shim()
    if shim:
        # find_docker() reads this env var before PATH, and caches its answer
        # on first use — which happens on the first terminal call, after
        # plugins have loaded. Setting it here is therefore sufficient.
        os.environ["HERMES_DOCKER_BINARY"] = shim
        logger.info("HERMES_DOCKER_BINARY -> %s", shim)
    else:
        logger.warning(
            "Apple Container shim not found (looked in %s and %s) and "
            "HERMES_DOCKER_BINARY is unset — Hermes will fall back to PATH, "
            "which cannot drive Apple Container.", BUNDLED_SHIM, LEGACY_SHIM,
        )


def _install() -> None:
    _ensure_shim()
    try:
        from tools import terminal_tool
    except Exception as e:
        logger.warning("could not import terminal_tool: %s", e)
        return
    if project_scope.install(terminal_tool):
        logger.info("per-project container scoping active (Apple Container)")


def _on_session_start(*args, **kwargs):
    # Re-assert on session start: cheap and idempotent, and it recovers if
    # terminal_tool was reloaded after plugin load.
    _install()
    return None


def register(ctx) -> None:
    _install()
    ctx.register_hook("on_session_start", _on_session_start)
