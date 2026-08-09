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

# The shim ships inside this plugin and is used from where the plugin is
# installed — a real file under ~/.hermes/plugins/, never a symlink back into a
# checkout. Installing the plugin is therefore the whole install: no second
# step, no ~/.hermes/.env edit, and nothing that breaks if the repo is moved or
# deleted.
BUNDLED_SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker-wrapper")


def _usable_shim() -> str:
    """Path to the plugin's own shim, or "" if it is missing/unusable."""
    if not os.path.exists(BUNDLED_SHIM):
        return ""
    if not os.access(BUNDLED_SHIM, os.X_OK):
        # A file copied by hand (or unpacked from an archive) can lose its
        # executable bit, and find_docker() skips anything not executable —
        # so it would be silently ignored. Fix it rather than warn.
        try:
            os.chmod(BUNDLED_SHIM, os.stat(BUNDLED_SHIM).st_mode | 0o111)
            logger.info("made shim executable: %s", BUNDLED_SHIM)
        except OSError as e:
            logger.warning(
                "shim %s is not executable and chmod failed: %s", BUNDLED_SHIM, e
            )
            return ""
    return BUNDLED_SHIM


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
            "Apple Container shim missing from the plugin (%s) and "
            "HERMES_DOCKER_BINARY is unset — Hermes will fall back to PATH, "
            "which cannot drive Apple Container. Reinstall the plugin.",
            BUNDLED_SHIM,
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
