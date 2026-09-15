"""Per-project container scoping for Hermes Agent.

Canonical source. ``install-plugins.sh`` copies this into each plugin
directory, so edit it here — never the copies.

What it does
------------
Hermes collapses every chat session's ``task_id`` to ``"default"``
(``tools/terminal_tool._resolve_container_task_id``), and
``_active_environments`` is keyed on that. One environment object therefore
serves every project in a gateway process, and because the cached object is
reused directly Hermes never shells out to ``docker`` again — so a docker-level
shim cannot influence which container a project gets.

This module wraps that function so the returned id carries the project, giving
each project its own environment, container and ``/workspace``.

Resolving the project
---------------------
Two signals, in order:

1. **The session's own cwd** (``state.db``, ``sessions.cwd``), matched against
   the project folders in ``projects.db``. This is per-session, so two projects
   executing *concurrently* resolve independently.

2. **The globally active project** (``projects.db``, ``project_meta.active_id``)
   as a fallback when the session has no usable cwd — e.g. a brand-new session
   whose first row has not been written yet.

Signal 1 is what makes concurrent execution correct. Relying on the global
``active_id`` alone would make two simultaneous sessions in different projects
resolve to whichever project happened to be active at that instant, and share a
container.

Longest-prefix matching is deliberate: with folders ``…/Alpha`` and
``…/Alpha/nested/Subproject`` registered as separate projects, a
session inside the latter must resolve to ``subproject``, not ``alpha``.

Profiles
--------
Hermes keys containers ``default`` (root home) or ``profile:<name>``. The
process running the gateway may be homed to the default profile even while it
serves another profile's sessions (the launchd gateway is a supervised child
and deliberately ignores the sticky ``active_profile``), so resolution NEVER
trusts the process-wide ``$HERMES_HOME`` alone: every DB read is scoped by the
session's own base key. A ``profile:<name>`` session reads that profile's
``state.db``/``projects.db`` — its cwd, its project folders, its *active*
project — and yields None instead of guessing with the default profile's
active project. Otherwise a profile session would resolve (and mount, via the
shim) a folder belonging to the default profile.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

# _resolve_container_task_id runs on every terminal and file-tool call, so the
# two SQLite reads are cached briefly. Short enough that switching projects
# feels immediate; long enough that a burst of tool calls in one turn does not
# re-query per call.
_CACHE_TTL = 2.0

# A fallback to the global active_id is a guess: it means the session's row
# hasn't been written to state.db yet (rows are written asynchronously, so a
# brand-new chat's first tool call can arrive first). Caching that guess for
# the full TTL pins the wrong project for two seconds — long enough to create
# a container and run the first command in another project's workspace.
# Re-check almost immediately instead, so the real answer wins as soon as the
# row lands.
_FALLBACK_CACHE_TTL = 0.2

# One entry per session id, so an unbounded dict grows for the lifetime of the
# gateway. Prune expired entries once it exceeds this, and hard-trim the oldest
# if they are all still live.
_CACHE_MAX = 512

_cache: dict = {}
_cache_lock = threading.Lock()

# The project folder list changes only when projects are added or moved, so it
# is cached against projects.db's (mtime, size) instead of being re-queried on
# every resolution. Paths are realpath'd once here rather than per call.
_folders_cache: dict = {}
_folders_lock = threading.Lock()

# Sticky session → project bindings.
#
# Hermes writes sessions.cwd only after a terminal command settles, so a
# session's *first* call has no per-session signal at all (origin_json and
# git_repo_root are empty too) and has to fall back to the global active_id.
# Without a binding, every later call would re-read that global value — so a
# session could silently change container mid-conversation just because the
# user switched projects in the UI. Binding the first answer keeps a session
# on one container for its lifetime.
#
# This does not make two *brand-new* sessions in different projects safe: while
# neither has a recorded cwd there is genuinely nothing to tell them apart, and
# both bind to whatever is active. A recorded cwd always overrides the binding,
# so such a session corrects itself as soon as Hermes writes one.
_BINDINGS_MAX = 512
_bindings: dict = {}
_bindings_lock = threading.Lock()


def _bind(task_id: Optional[str], slug: str) -> None:
    if not task_id or not slug:
        return
    with _bindings_lock:
        _bindings[task_id] = slug
        if len(_bindings) > _BINDINGS_MAX:
            # Plain FIFO trim; bindings carry no timestamp and the cap only
            # exists so a long-lived gateway cannot grow this without bound.
            for key in list(_bindings)[: len(_bindings) - _BINDINGS_MAX]:
                _bindings.pop(key, None)


def _bound(candidates) -> Optional[str]:
    with _bindings_lock:
        for cand in candidates:
            slug = _bindings.get(cand)
            if slug:
                return slug
    return None


def _prune_cache_locked(now: float) -> None:
    """Drop expired entries; if still oversized, drop the oldest. Caller holds
    the lock."""
    for key, (stamp, _slug, source) in list(_cache.items()):
        ttl = _FALLBACK_CACHE_TTL if source == "active" else _CACHE_TTL
        if now - stamp >= ttl:
            _cache.pop(key, None)
    if len(_cache) > _CACHE_MAX:
        for key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[
            : len(_cache) - _CACHE_MAX
        ]:
            _cache.pop(key, None)


def _hermes_home() -> str:
    return os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")


def _hermes_root() -> str:
    """The root that contains per-profile homes, or the process home itself.

    ``$HERMES_HOME`` resolves to exactly one home per process; the gateway
    that serves a session may be homed to the *default* profile even when the
    session belongs to another profile (the launchd gateway is a supervised
    child and deliberately ignores the sticky ``active_profile``). The profile
    is carried in the container key instead (``default`` vs ``profile:<name>``)
    — so derive the per-profile home from *that*, never only from the
    process-wide env var.
    """
    home = _hermes_home()
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def _profile_of_base(base) -> Optional[str]:
    """The profile named by a container base key, or None.

    ``"default"``/None mean the root home; ``"profile:work"`` means
    ``<root>/profiles/work`` regardless of which home this process runs under.
    """
    if isinstance(base, str) and base.startswith("profile:"):
        name = base.split(":", 1)[1].strip()
        return name or None
    return None


def _home_for_profile(profile: Optional[str]) -> Optional[str]:
    """Home dir of *profile* (``<root>/profiles/<name>``), or None (root home)."""
    if not profile or profile == "default":
        return None
    return os.path.join(_hermes_root(), "profiles", profile)


def _db_path(kind: str, base) -> str:
    """``<kind>.db`` from the home the *base* key belongs to.

    A ``profile:<name>`` base reads that profile's DB even when this process
    is homed elsewhere; anything else reads the process home (unchanged
    behaviour). This is what keeps a profile session from ever resolving
    against — or worse, mounting — the default profile's project folders.
    """
    profile = _profile_of_base(base)
    home = _home_for_profile(profile) or _hermes_home()
    return os.path.join(home, kind + ".db")


def _connect_ro(db_path: str) -> Optional[sqlite3.Connection]:
    """Open *db_path* for reading, or return None.

    Both databases run in WAL mode. A ``mode=ro`` open needs the ``-shm``
    sidecar, and when that has been checkpointed away SQLite cannot create it
    and fails with "unable to open database file" — so fall back to a normal
    connection, which is what Hermes' own ``projects_db.connect()`` uses.

    ``immutable=1`` would also open without the sidecar, but it bypasses the
    WAL and would read stale rows. Never use it here: a stale ``active_id`` or
    a missing session row is exactly the failure this must avoid.
    """
    if not os.path.exists(db_path):
        return None
    for dsn, kwargs in ((f"file:{db_path}?mode=ro", {"uri": True}), (db_path, {})):
        try:
            return sqlite3.connect(dsn, timeout=2.0, **kwargs)
        except sqlite3.Error:
            continue
    return None


def sanitize(value: str) -> str:
    return _LABEL_SAFE_RE.sub("_", str(value))[:40]


def _session_cwd(task_id: str, base=None) -> Optional[str]:
    """Host cwd recorded for *task_id*, or None.

    ``sessions.id`` uses the same value the terminal tool receives as
    ``task_id`` (e.g. ``20260807_105303_df4fdd``). ``session_key`` is checked
    too since gateway sessions are registered under it.

    The DB read follows *base*: a ``profile:<name>`` session reads that
    profile's ``state.db`` — a session's cwd lives in its own profile, not in
    whichever home this process happens to run under.
    """
    conn = _connect_ro(_db_path("state", base))
    if conn is None:
        return None
    try:
        # Two queries, not "id = ? OR session_key = ?": the OR cannot use the
        # primary-key index, so it degrades to a table scan. The sessions
        # table is small in practice, so this is a safeguard for large
        # installs rather than a measured win here — the id lookup hits the
        # PK and answers almost always; session_key is a fallback because the
        # gateway registers some sessions under it.
        row = conn.execute(
            "SELECT cwd FROM sessions WHERE id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if not row or not row[0]:
            row = conn.execute(
                "SELECT cwd FROM sessions WHERE session_key = ? LIMIT 1", (task_id,)
            ).fetchone()
    except sqlite3.Error as e:
        logger.debug("session cwd lookup failed: %s", e)
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return os.path.abspath(os.path.expanduser(str(row[0])))


def _project_folders(base=None):
    """[(slug, abspath, realpath)], cached against the owning DB's mtime+size.

    The DB follows *base* (see ``_db_path``), so a ``profile:<name>`` session
    matches only its own profile's project folders.
    """
    db = _db_path("projects", base)
    try:
        st = os.stat(db)
        stamp = (db, st.st_mtime_ns, st.st_size)
    except OSError:
        return []

    cache_key = "v" + (base or "")
    with _folders_lock:
        hit = _folders_cache.get(cache_key)
        if hit and hit[0] == stamp:
            return hit[1]

    conn = _connect_ro(db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT p.slug, f.path FROM project_folders f "
            "JOIN projects p ON p.id = f.project_id "
            "UNION "
            "SELECT slug, primary_path FROM projects "
            "WHERE primary_path IS NOT NULL AND primary_path != ''"
        ).fetchall()
    except sqlite3.Error as e:
        logger.debug("project folder lookup failed: %s", e)
        return []
    finally:
        conn.close()

    folders = []
    for slug, folder in rows:
        if not folder:
            continue
        root = os.path.abspath(os.path.expanduser(str(folder)))
        # realpath too, so a session cwd reached through a symlinked parent
        # still matches its project.
        try:
            real = os.path.realpath(root)
        except OSError:
            real = root
        folders.append((slug, root, real))

    with _folders_lock:
        _folders_cache[cache_key] = (stamp, folders)
    return folders


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _project_for_path(path: str, base=None) -> Optional[str]:
    """Slug of the project owning *path*, by longest-prefix match.

    Longest wins so a project nested inside another (…/Alpha/…/Subproject
    within …/Alpha) resolves to the inner one. The folder list follows *base*,
    so a profile session never matches the default profile's projects.
    """
    folders = _project_folders(base)
    if not folders:
        return None
    try:
        real_path = os.path.realpath(path)
    except OSError:
        real_path = path

    best_slug, best_len = None, -1
    for slug, root, real_root in folders:
        if _under(path, root):
            match_len = len(root)
        elif _under(real_path, real_root):
            match_len = len(real_root)
        else:
            continue
        if match_len > best_len:
            best_slug, best_len = slug, match_len
    return best_slug


def _active_project_slug(base=None) -> Optional[str]:
    conn = _connect_ro(_db_path("projects", base))
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT p.slug FROM project_meta m JOIN projects p ON p.id = m.value "
            "WHERE m.key = 'active_id'"
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug("active project lookup failed: %s", e)
        return None
    finally:
        conn.close()
    return row[0] if row and row[0] else None


# Set at tool entry, where both task_id and session_id are in scope.
# _resolve_container_task_id() only receives task_id, and Hermes' own docstring
# says the top-level agent passes task_id=None — in which case the session id
# is the only per-session discriminator available, and without it concurrent
# projects would collapse onto the global active_id and share a container.
_current = threading.local()

# The unwrapped upstream resolver, set by install(). project_path_for_task
# consults it to learn a session's base key ("default" / "profile:<name>")
# without reimplementing Hermes' isolation rules.
_original_resolver = None


def note_ids(task_id: Optional[str], session_id: Optional[str]) -> None:
    _current.ids = (task_id, session_id)


def clear_ids(previous) -> None:
    _current.ids = previous


def current_ids():
    return getattr(_current, "ids", None)


def _candidate_ids(task_id: Optional[str]):
    """Session identifiers to try, most specific first, de-duplicated."""
    seen, out = set(), []
    for cand in (task_id,) + (current_ids() or (None, None)):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def resolve_project_slug(task_id: Optional[str], base=None) -> Tuple[Optional[str], str]:
    """Return ``(slug, source)`` for *task_id*. ``slug`` is None if unknown.

    ``base`` is the container key Hermes resolved for this session
    (``"default"`` or ``profile:<name>``). It selects whose DBs are read: a
    ``profile:<name>`` session resolves against that profile's ``state.db``
    and ``projects.db``, never the process home's — a session in another
    profile must not be able to resolve (or mount) the default profile's
    active project. ``None`` keeps the historical process-home behaviour.

    ``source`` is "session", "active" or "none" — reported so the reason a
    container was chosen is visible in the logs.
    """
    candidates = _candidate_ids(task_id)
    key = "|".join(candidates) + "::" + (base or "")
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit:
            # A bound answer is as stable as a session-derived one; only a
            # bare active_id guess is re-checked aggressively, so a session
            # picks up its real cwd as soon as one is written.
            ttl = _FALLBACK_CACHE_TTL if hit[2] == "active" else _CACHE_TTL
            if now - hit[0] < ttl:
                return hit[1], hit[2]

    slug, source = None, "none"

    # 1. The session's own cwd — authoritative, and per-session, so concurrent
    #    projects resolve independently. Overrides any earlier binding, which
    #    lets a session that had to guess correct itself once Hermes records a
    #    cwd for it.
    for cand in candidates:
        cwd = _session_cwd(cand, base)
        if not cwd:
            continue
        found = _project_for_path(cwd, base)
        if found:
            slug, source = found, "session"
            break

    # 2. What this session resolved to before. Keeps it on one container even
    #    if the globally active project changes underneath it.
    if slug is None:
        found = _bound(candidates)
        if found:
            slug, source = found, "bound"

    # 3. Nothing session-specific exists yet: guess the active project and
    #    remember it, so the guess is made once rather than re-rolled per call.
    #    The active project comes from the SAME home as the base key — for a
    #    profile session that is the profile's own DB, so a fresh profile with
    #    no active project yields None instead of leaking the default
    #    profile's active project into the mount decision.
    if slug is None:
        found = _active_project_slug(base)
        if found:
            slug, source = found, "active"

    if slug and source in ("session", "active"):
        for cand in candidates:
            _bind(cand, slug)

    with _cache_lock:
        _cache[key] = (now, slug, source)
        if len(_cache) > _CACHE_MAX:
            _prune_cache_locked(now)
    return slug, source


def project_path_for_task(task_id: Optional[str], base=None) -> Optional[str]:
    """Primary directory of the project owning *task_id*, or None.

    Used by the Docker plugin to make the ``/workspace`` bind mount follow the
    project. The Apple Container variant does not need this — its shim rewrites
    the mount at ``docker run`` time.

    *base* is the session's container key; when omitted it is derived from the
    wrapped ``_resolve_container_task_id`` so a profile session reads the
    profile's own projects.db.
    """
    if base is None and _original_resolver is not None:
        try:
            base = _original_resolver(task_id)
        except Exception:
            base = None
    slug, _ = resolve_project_slug(task_id, base)
    if not slug:
        return None
    conn = _connect_ro(_db_path("projects", base))
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(NULLIF(p.primary_path, ''), "
            "  (SELECT f.path FROM project_folders f "
            "   WHERE f.project_id = p.id ORDER BY f.is_primary DESC LIMIT 1)) "
            "FROM projects p WHERE p.slug = ?",
            (slug,),
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug("project path lookup failed: %s", e)
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    path = os.path.abspath(os.path.expanduser(str(row[0])))
    return path if os.path.isdir(path) else None


def scoped_task_id(task_id: Optional[str], base: str) -> str:
    """Scope *base* by the project owning *task_id*.

    Idempotent, so an already-scoped value passed back in is unchanged.
    """
    slug, source = resolve_project_slug(task_id, base)
    if not slug:
        return base
    slug = sanitize(slug)
    if base.endswith("." + slug):
        return base
    scoped = f"{base}.{slug}"
    profile = _profile_of_base(base)
    if source == "active":
        # Fell back to the active project of the base's home. Correct for a
        # single session, but concurrent sessions in different projects would
        # both land here and share a container — so say so rather than fail
        # quietly.
        logger.info(
            "project scope: task_id=%r %s-> %s via global active_id "
            "(no session cwd found; concurrent projects may collide)",
            task_id,
            f"(profile {profile}) " if profile else "",
            scoped,
        )
    else:
        logger.info(
            "project scope: task_id=%r %s-> %s via %s",
            task_id,
            f"(profile {profile}) " if profile else "",
            scoped, source,
        )
    return scoped


def install(terminal_tool) -> bool:
    """Wrap ``terminal_tool._resolve_container_task_id`` in place.

    Wrapping rather than replacing means Hermes' own logic still runs first —
    including the RL/benchmark isolation-key branch, which must keep returning
    its raw task_id untouched. Shared return values ("default", "profile:<name>")
    get scoped; per-session and explicit-shared values are left alone, so an
    upstream change to the isolation rules is inherited automatically instead
    of being silently overridden.

    Idempotent: a second call is a no-op.
    """
    original = getattr(terminal_tool, "_resolve_container_task_id", None)
    if original is None:
        logger.warning(
            "per-project scoping not installed: "
            "terminal_tool._resolve_container_task_id is missing "
            "(Hermes changed upstream)"
        )
        return False
    if getattr(original, "_hermes_containers_wrapped", False):
        return True
    global _original_resolver
    _original_resolver = original

    def wrapped(task_id=None):
        base = original(task_id)
        # Per-session isolation keys ("session:…", "shared:…") and raw
        # isolation-override ids already have their own sandbox — leave
        # them alone.  "default" and "profile:<name>" are *shared* keys
        # that collapse every project onto one container, so scope them
        # by project to partition the cache.
        if base != "default" and not (
            isinstance(base, str) and base.startswith("profile:")
        ):
            return base
        try:
            return scoped_task_id(task_id, base)
        except Exception as e:
            logger.warning("per-project scoping failed, using %r: %s", base, e)
            return base

    wrapped._hermes_containers_wrapped = True  # type: ignore[attr-defined]
    wrapped._hermes_containers_original = original  # type: ignore[attr-defined]
    terminal_tool._resolve_container_task_id = wrapped

    _install_id_capture(terminal_tool)
    return True


def _install_id_capture(terminal_tool) -> bool:
    """Record (task_id, session_id) at tool entry for the current thread.

    ``_resolve_container_task_id`` only receives ``task_id``, which the
    top-level agent may pass as None. ``terminal_tool()`` receives
    ``session_id`` as well, and runs on the same thread, so capturing both here
    gives the resolver a per-session discriminator in either case.

    Without this, concurrent sessions whose ``task_id`` is None would all fall
    through to the global ``active_id`` and share one container — the exact
    failure this design exists to prevent.
    """
    entry = getattr(terminal_tool, "terminal_tool", None)
    if entry is None or getattr(entry, "_hermes_containers_wrapped", False):
        return False

    def wrapped_entry(*args, **kwargs):
        previous = current_ids()
        try:
            note_ids(kwargs.get("task_id"), kwargs.get("session_id"))
        except Exception:
            pass
        try:
            return entry(*args, **kwargs)
        finally:
            clear_ids(previous)

    wrapped_entry._hermes_containers_wrapped = True  # type: ignore[attr-defined]
    wrapped_entry._hermes_containers_original = entry  # type: ignore[attr-defined]
    terminal_tool.terminal_tool = wrapped_entry
    return True
