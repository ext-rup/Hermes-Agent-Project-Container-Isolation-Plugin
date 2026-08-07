"""hermes-projects-docker — per-project containers on real Docker.

Scopes Hermes' container cache by project, so each project chat gets its own
container with its own project directory at /workspace. See ``project_scope``
for why this cannot be done at the docker-CLI level.

Unlike the Apple Container variant this needs no shim: Docker supports labels,
``ps --filter`` and Go templates natively, so Hermes' own container reuse works
as designed once the task ids are distinct.

It does need the workspace mount to follow the project. Hermes takes the
``/workspace`` source from ``terminal.docker_volumes`` or the process-wide
``TERMINAL_CWD``, neither of which is per-project — so this plugin sets
``host_cwd`` per project via a task env override, and requires:

    terminal.docker_mount_cwd_to_workspace: true

Enable exactly one of hermes-projects-apple / hermes-projects-docker.
"""

from __future__ import annotations

import logging
import os

from . import project_scope

logger = logging.getLogger(__name__)


def _check_docker_binary() -> None:
    """Warn if Hermes is still pointed at the Apple Container shim.

    Switching from the Apple backend leaves HERMES_DOCKER_BINARY set in
    ~/.hermes/.env. find_docker() checks it before PATH, so Hermes would keep
    driving Apple Container while this Docker plugin manages the scoping —
    a confusing half-configured state.
    """
    binary = (os.environ.get("HERMES_DOCKER_BINARY") or "").strip()
    if not binary:
        return
    name = os.path.basename(binary)
    if "docker-wrapper" in name or "apple" in name.lower():
        logger.warning(
            "HERMES_DOCKER_BINARY=%s looks like the Apple Container shim, but "
            "the Docker backend plugin is enabled. Remove that line from "
            "~/.hermes/.env, or switch back with ./install-plugins.sh apple.",
            binary,
        )


def _check_mount_config() -> None:
    """Warn when the workspace mount cannot follow the project."""
    try:
        from hermes_cli.config import load_config

        terminal = (load_config() or {}).get("terminal") or {}
    except Exception:
        return
    if not terminal.get("docker_mount_cwd_to_workspace"):
        logger.warning(
            "terminal.docker_mount_cwd_to_workspace is false — each project "
            "will get its own container, but /workspace will not point at the "
            "project directory. Set it true: "
            "hermes config set terminal.docker_mount_cwd_to_workspace true"
        )
    for vol in terminal.get("docker_volumes") or []:
        if isinstance(vol, str) and vol.rstrip("/").endswith(":/workspace"):
            logger.warning(
                "terminal.docker_volumes pins /workspace to %r. That is a "
                "fixed host path, so every project would mount it. Remove the "
                "entry so the per-project mount applies.", vol,
            )


def _install_mount_override() -> bool:
    """Make ``host_cwd`` follow the session's project.

    ``_get_env_config()`` reads ``host_cwd`` from the process-wide
    ``TERMINAL_CWD``, so every container would mount the same directory. Wrap
    the environment factory to substitute the project's own path instead.
    """
    try:
        from tools import terminal_tool
    except Exception as e:
        logger.warning("could not import terminal_tool: %s", e)
        return False

    original = getattr(terminal_tool, "_create_environment", None)
    if original is None:
        logger.warning(
            "workspace mount override not installed: "
            "terminal_tool._create_environment is missing (Hermes changed "
            "upstream). Containers will still be per-project, but /workspace "
            "may not follow."
        )
        return False
    if getattr(original, "_hermes_containers_wrapped", False):
        return True

    def wrapped(*args, **kwargs):
        task_id = kwargs.get("task_id")
        try:
            path = project_scope.project_path_for_task(task_id)
            if path:
                kwargs["host_cwd"] = path
        except Exception as e:
            logger.warning("could not resolve project path for %r: %s", task_id, e)
        return original(*args, **kwargs)

    wrapped._hermes_containers_wrapped = True  # type: ignore[attr-defined]
    wrapped._hermes_containers_original = original  # type: ignore[attr-defined]
    terminal_tool._create_environment = wrapped
    return True


def _install() -> None:
    try:
        from tools import terminal_tool
    except Exception as e:
        logger.warning("could not import terminal_tool: %s", e)
        return
    if project_scope.install(terminal_tool):
        logger.info("per-project container scoping active (Docker)")
    _install_mount_override()


def _on_session_start(*args, **kwargs):
    _install()
    return None


def register(ctx) -> None:
    _check_docker_binary()
    _check_mount_config()
    _install()
    ctx.register_hook("on_session_start", _on_session_start)
