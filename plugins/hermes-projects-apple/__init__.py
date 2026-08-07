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

# Installed alongside this package by install-plugins.sh.
DEFAULT_SHIM = os.path.expanduser("~/.hermes/docker-wrapper")


def _ensure_shim() -> None:
    """Point HERMES_DOCKER_BINARY at the shim if the user has not."""
    current = (os.environ.get("HERMES_DOCKER_BINARY") or "").strip()
    if current:
        if not os.path.exists(current):
            logger.warning(
                "HERMES_DOCKER_BINARY=%s does not exist; Hermes will fall back "
                "to PATH and probably find a Docker CLI that cannot drive "
                "Apple Container.", current,
            )
        return
    if os.path.exists(DEFAULT_SHIM):
        os.environ["HERMES_DOCKER_BINARY"] = DEFAULT_SHIM
        logger.info("HERMES_DOCKER_BINARY -> %s", DEFAULT_SHIM)
    else:
        logger.warning(
            "Apple Container shim not found at %s and HERMES_DOCKER_BINARY is "
            "unset — per-project mounts will not be applied.", DEFAULT_SHIM,
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
