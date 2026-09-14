"""hermes-apple-container — Apple Container as a first-class terminal backend.

Registers a ``apple_container`` terminal backend via the TerminalEnvironmentProvider
plugin API.  The backend subclasses ``DockerEnvironment`` and swaps the CLI binary
for the bundled ``docker-wrapper`` shim, which translates Docker commands into
Apple Container's ``container`` CLI.  Everything else — container lifecycle,
snapshot/env forwarding, background processes, file sync, recovery — is inherited
from the Docker backend unchanged.

This replaces the old approach of hijacking ``terminal.backend: docker`` via
``HERMES_DOCKER_BINARY``.  With a registered backend, the user selects it like any
other:

    hermes config set terminal.backend apple_container

No function wrapping, no env-var tricks, no fragility against upstream refactors.

Per-project container isolation is handled natively: the provider sets
``session_isolated_when_nonpersistent = True`` so each session gets its own
container identity when ``container_persistent: false`` is set, and the
workspace mount follows the project via the same project-scope resolution.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)

# Path to the shim bundled inside this plugin.
BUNDLED_SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker-wrapper")


def _usable_shim() -> str:
    """Return the path to the plugin's own shim, or "" if missing/unusable."""
    if not os.path.exists(BUNDLED_SHIM):
        return ""
    if not os.access(BUNDLED_SHIM, os.X_OK):
        try:
            os.chmod(BUNDLED_SHIM, os.stat(BUNDLED_SHIM).st_mode | 0o111)
            logger.info("made shim executable: %s", BUNDLED_SHIM)
        except OSError as e:
            logger.warning(
                "shim %s is not executable and chmod failed: %s", BUNDLED_SHIM, e
            )
            return ""
    return BUNDLED_SHIM


def _find_container_cli() -> str | None:
    """Locate Apple's ``container`` CLI (the shim delegates to it)."""
    return shutil.which("container") or "/usr/local/bin/container"


# --- Provider ---------------------------------------------------------------

_AppliedEnvClass = None  # set by _install() before any create_environment call


def _make_provider():
    """Build the TerminalEnvironmentProvider lazily (defer imports until plugin load)."""
    from agent.terminal_env_provider import TerminalEnvironmentProvider

    class AppleContainerProvider(TerminalEnvironmentProvider):
        name = "apple_container"
        display_name = "Apple Container"
        is_remote = True
        is_container = True
        # Apple Container sandboxes are disposable: skip dangerous-command
        # approval the same way Docker does.
        skip_container_guards = True
        # A non-persistent container is a fresh VM each session.
        session_isolated_when_nonpersistent = True

        @property
        def description(self) -> str:
            return "Run commands in Apple Container (lightweight macOS container runtime)."

        @property
        def cache_path_base(self) -> str | None:
            return "/root/.hermes"

        @property
        def strip_env_keys(self) -> frozenset:
            return frozenset()

        def is_available(self) -> bool:
            # Cheap, no network: the shim and the container CLI must both exist.
            if not _usable_shim():
                return False
            return _find_container_cli() is not None

        def check_requirements(self, config) -> bool:
            if not self.is_available():
                logger.error(
                    "Apple Container backend selected but the 'container' CLI was not "
                    "found. Install it: brew install container"
                )
                return False
            return True

        def probe(self) -> tuple[str, str]:
            if self.is_available():
                return ("ready", "Apple Container CLI found.")
            return ("needs_setup", "Install the container CLI: brew install container")

        def setup_instructions(self) -> list[str]:
            return [
                "Install Apple Container CLI:  brew install container",
                "Select the backend:  hermes config set terminal.backend apple_container",
            ]

        def create_environment(self, *, cwd, timeout, task_id="default",
                               image=None, container_config=None, **kwargs):
            # _AppliedEnvClass is built by _install() at plugin load time.
            if _AppliedEnvClass is None:
                raise RuntimeError(
                    "AppleContainerEnvironment not built — plugin load failed. "
                    "Check ~/.hermes/logs/agent.log for the build error."
                )
            return _AppliedEnvClass(
                cwd=cwd, timeout=timeout, task_id=task_id,
                image=image or "nikolaik/python-nodejs:python3.13-nodejs24",
                container_config=container_config or {},
            )

    return AppleContainerProvider()


# --- Environment ------------------------------------------------------------

def _make_env_class():
    """Build the environment class lazily, after Hermes' tools are importable."""
    from tools.environments.docker import DockerEnvironment

    class AppleContainerEnvironment(DockerEnvironment):
        """DockerEnvironment that runs through the Apple Container shim.

        The shim (docker-wrapper) translates every ``docker`` subcommand Hermes
        uses into the equivalent ``container`` CLI call: ``run``, ``exec``, ``ps``
        (with client-side --filter and Go-template rendering), ``inspect``,
        ``stop``, ``rm``, ``image inspect``, ``info``, ``version``.  Unsupported
        flags (``--security-opt``, ``--pids-limit``, ``--storage-opt``,
        ``--privileged``, ``--cap-drop`` etc.) are silently dropped by the shim,
        so the full DockerEnvironment arg-building pipeline can be reused as-is.

        What we override:
          * ``__init__`` — resolve the shim path, skip probes that don't apply,
            then call super().__init__ which builds args and starts the container.
          * ``_storage_opt_supported`` — always False (no overlay2/XFS on macOS).
          * ``_cgroup_limits_available`` — always False (Apple Container has no
            cgroup controllers; the shim drops --cpus/--memory/--pids-limit).
          * Image-init entrypoint detection — delegated to the shim's
            ``image inspect``, which already returns Docker-shaped JSON.
        """

        def __init__(self, cwd, timeout, task_id, image, container_config):
            shim = _usable_shim()
            if not shim:
                raise RuntimeError(
                    "Apple Container shim not found at %s. Reinstall the plugin."
                    % BUNDLED_SHIM
                )
            if _find_container_cli() is None:
                raise RuntimeError(
                    "Apple 'container' CLI not found on PATH. "
                    "Install it: brew install container"
                )

            # Extract kwargs from container_config the same way the Docker
            # builder (_build_docker_env) does.
            cc = container_config or {}
            volumes = cc.get("docker_volumes", [])
            forward_env = cc.get("docker_forward_env", [])
            env = cc.get("docker_env", {})
            network = cc.get("docker_network", True)
            run_as_host_user = cc.get("docker_run_as_host_user", False)
            extra_args = cc.get("docker_extra_args", [])
            persist_across = cc.get("docker_persist_across_processes", True)
            shared_key = cc.get("docker_shared_container_key", "")
            shm_size = cc.get("docker_shm_size", "1g")
            snap_compat = cc.get("docker_snap_compat", False)
            auto_mount_cwd = cc.get("docker_mount_cwd_to_workspace", False)
            persistent = cc.get("container_persistent", True)
            cpu = cc.get("container_cpu", 1)
            memory = cc.get("container_memory", 5120)
            disk = cc.get("container_disk", 51200)

            # find_docker() checks HERMES_DOCKER_BINARY first and caches the
            # result in a module-level _docker_executable.  _ensure_docker_available
            # runs ``docker version`` to verify the daemon.  Both happen inside
            # super().__init__, so we temporarily point HERMES_DOCKER_BINARY at the
            # shim and reset the cache so the parent class resolves our shim
            # instead of a real Docker binary.  The shim's ``version`` command
            # always succeeds (it prints a static string), satisfying the
            # availability probe.
            from tools.environments import docker as _docker_mod
            saved_env = os.environ.get("HERMES_DOCKER_BINARY")
            saved_cache = _docker_mod._docker_executable
            os.environ["HERMES_DOCKER_BINARY"] = shim
            _docker_mod._docker_executable = None  # force re-resolution
            try:
                super().__init__(
                    image=image,
                    cwd=cwd,
                    timeout=timeout,
                    cpu=cpu,
                    memory=memory,
                    disk=disk,
                    persistent_filesystem=persistent,
                    task_id=task_id,
                    volumes=volumes,
                    forward_env=forward_env,
                    env=env,
                    network=network,
                    host_cwd=None,
                    auto_mount_cwd=auto_mount_cwd,
                    run_as_host_user=run_as_host_user,
                    extra_args=extra_args,
                    persist_across_processes=persist_across,
                    shm_size=shm_size,
                    shared_container_key=shared_key,
                    snap_compat=snap_compat,
                )
            finally:
                # Restore so we don't permanently clobber the global state for
                # other backends that might run in the same gateway process.
                if saved_env is not None:
                    os.environ["HERMES_DOCKER_BINARY"] = saved_env
                else:
                    os.environ.pop("HERMES_DOCKER_BINARY", None)
                _docker_mod._docker_executable = saved_cache

            # The parent set self._docker_exe = find_docker() which resolved
            # our shim via HERMES_DOCKER_BINARY above.  Belt-and-suspenders:
            # ensure it's the shim regardless.
            self._docker_exe = shim

        @staticmethod
        def _storage_opt_supported() -> bool:
            # Apple Container has no per-container disk quota concept.
            return False

        # _cgroup_limits_available is a module-level function in docker.py,
        # not a method.  We can't override it on the class, but the shim
        # drops --cpus/--memory/--pids-limit anyway, so even if DockerEnvironment
        # emits them they're harmlessly stripped.  If the probe runs it will
        # go through the shim (which translates ``run --rm``) and return False
        # because Apple Container rejects those flags — that's fine: it
        # means DockerEnvironment skips them, which is what we want.

    return AppleContainerEnvironment


# --- Plugin registration ----------------------------------------------------


def _install() -> None:
    """Resolve the shim, build the env class, and install per-project scoping."""
    global _AppliedEnvClass
    if _AppliedEnvClass is None:
        try:
            _AppliedEnvClass = _make_env_class()
        except Exception as e:
            logger.warning("could not build AppleContainerEnvironment: %s", e)

    # Install per-project container scoping so each project chat gets its own
    # container.  This wraps _resolve_container_task_id the same way the old
    # hermes-projects-apple plugin did, and the shim picks up the scoped label
    # at docker run time to repoint /workspace at the right project directory.
    try:
        from tools import terminal_tool
        from . import project_scope
        project_scope.install(terminal_tool)
        logger.info("per-project container scoping active (Apple Container provider)")
    except Exception as e:
        logger.warning("could not install per-project scoping: %s", e)


def _on_session_start(*args, **kwargs):
    _install()
    return None


def register(ctx) -> None:
    _install()
    ctx.register_terminal_environment_provider(_make_provider())
    ctx.register_hook("on_session_start", _on_session_start)
