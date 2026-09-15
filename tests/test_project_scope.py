"""Tests for per-project container scoping.

The important cases are the ones that decide which container a session gets:
per-session resolution (concurrency), longest-prefix folder matching, and
leaving RL/benchmark isolation ids alone.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "project_scope", os.path.join(REPO, "plugins", "project_scope.py")
)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def make_dbs(home, projects, sessions, active_id=None):
    """Build minimal projects.db + state.db mirroring Hermes' schemas."""
    pconn = sqlite3.connect(os.path.join(home, "projects.db"))
    pconn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, primary_path TEXT, created_at INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE project_folders (project_id TEXT NOT NULL, path TEXT NOT NULL,
            label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project_id, path));
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    for pid, slug, path in projects:
        pconn.execute(
            "INSERT INTO projects (id, slug, name, primary_path) VALUES (?,?,?,?)",
            (pid, slug, slug, path),
        )
        if path:
            pconn.execute(
                "INSERT INTO project_folders (project_id, path, is_primary) VALUES (?,?,1)",
                (pid, path),
            )
    if active_id:
        pconn.execute("INSERT INTO project_meta (key, value) VALUES ('active_id', ?)", (active_id,))
    pconn.commit()
    pconn.close()

    sconn = sqlite3.connect(os.path.join(home, "state.db"))
    sconn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, cwd TEXT)"
    )
    for sid, cwd in sessions:
        sconn.execute("INSERT INTO sessions (id, session_key, cwd) VALUES (?,'',?)", (sid, cwd))
    sconn.commit()
    sconn.close()


class ScopeTestBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HERMES_HOME"] = self.home
        ps._cache.clear()
        ps._folders_cache.clear()
        ps._bindings.clear()
        ps._original_resolver = None
        self.alpha = os.path.join(self.home, "Alpha")
        self.selling = os.path.join(self.alpha, "nested", "Subproject")
        self.beta = os.path.join(self.home, "Beta")
        for d in (self.alpha, self.selling, self.beta):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        os.environ.pop("HERMES_HOME", None)
        ps._cache.clear()
        ps._folders_cache.clear()
        ps._bindings.clear()
        ps._original_resolver = None


class TestSessionResolution(ScopeTestBase):
    def test_resolves_from_session_cwd(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-a", self.beta)],
            active_id="p1",
        )
        slug, source = ps.resolve_project_slug("sess-a")
        self.assertEqual(slug, "beta")
        self.assertEqual(source, "session")

    def test_concurrent_sessions_resolve_independently(self):
        """The whole point: two sessions at once must not share a container."""
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-a", self.alpha), ("sess-b", self.beta)],
            active_id="p1",
        )
        self.assertEqual(ps.scoped_task_id("sess-a", "default"), "default.alpha")
        self.assertEqual(ps.scoped_task_id("sess-b", "default"), "default.beta")

    def test_longest_prefix_wins(self):
        """A nested project must not be swallowed by its parent folder."""
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "subproject", self.selling)],
            [("sess-a", self.selling)],
        )
        slug, _ = ps.resolve_project_slug("sess-a")
        self.assertEqual(slug, "subproject")

    def test_falls_back_to_active_when_session_unknown(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha)],
            [],
            active_id="p1",
        )
        slug, source = ps.resolve_project_slug("no-such-session")
        self.assertEqual(slug, "alpha")
        self.assertEqual(source, "active")

    def test_cwd_outside_any_project_falls_back(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-a", "/tmp/somewhere-else")],
            active_id="p2",
        )
        slug, source = ps.resolve_project_slug("sess-a")
        self.assertEqual(slug, "beta")
        self.assertEqual(source, "active")

    def test_no_projects_yields_none(self):
        make_dbs(self.home, [], [("sess-a", self.beta)])
        slug, _ = ps.resolve_project_slug("sess-a")
        self.assertIsNone(slug)

    def test_missing_dbs_do_not_raise(self):
        slug, _ = ps.resolve_project_slug("sess-a")
        self.assertIsNone(slug)


class TestScopedTaskId(ScopeTestBase):
    def setUp(self):
        super().setUp()
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")

    def test_scopes(self):
        self.assertEqual(ps.scoped_task_id("s", "default"), "default.alpha")

    def test_idempotent(self):
        once = ps.scoped_task_id("s", "default")
        ps._cache.clear()
        ps._folders_cache.clear()
        self.assertEqual(ps.scoped_task_id("s", once), once)

    def test_unscoped_when_no_project(self):
        ps._cache.clear()
        ps._folders_cache.clear()
        os.environ["HERMES_HOME"] = tempfile.mkdtemp()
        self.assertEqual(ps.scoped_task_id("s", "default"), "default")


class TestInstall(ScopeTestBase):
    class FakeTerminalTool:
        def __init__(self, result):
            self._result = result
            self.calls = []

        def _resolve_container_task_id(self, task_id=None):
            self.calls.append(task_id)
            return self._result

    def _module(self, result):
        """A stand-in module object exposing _resolve_container_task_id."""
        import types

        mod = types.ModuleType("fake_terminal_tool")
        fake = self.FakeTerminalTool(result)
        mod._resolve_container_task_id = fake._resolve_container_task_id
        mod._fake = fake
        return mod

    def test_scopes_default_result(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        mod = self._module("default")
        self.assertTrue(ps.install(mod))
        self.assertEqual(mod._resolve_container_task_id("s"), "default.alpha")

    def test_leaves_isolation_task_ids_alone(self):
        """RL/benchmark rollouts return their raw task_id; must not be scoped."""
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        mod = self._module("benchmark-run-7")
        ps.install(mod)
        self.assertEqual(mod._resolve_container_task_id("s"), "benchmark-run-7")

    def test_scopes_profile_key_from_the_profiles_own_home(self):
        """'profile:work' must resolve against work's DBs, not the process home's.

        The process may be homed to the default profile while serving another
        profile's sessions. The project was created in profile work, so a
        session under base 'profile:work' must find it in
        <root>/profiles/work/projects.db — never in the default home.
        """
        root = self.home  # process homed to the DEFAULT profile
        work_home = os.path.join(root, "profiles", "work")
        os.makedirs(work_home, exist_ok=True)
        # The *default* profile has its own active project that must NOT win.
        make_dbs(root, [("p1", "alpha", self.alpha)], [], active_id="p1")
        # Profile work has its project and the session's cwd.
        work_alpha = os.path.join(work_home, "Alpha")
        os.makedirs(work_alpha, exist_ok=True)
        make_dbs(
            work_home,
            [("w1", "gamma", work_alpha)],
            [("s", work_alpha)],
            active_id="w1",
        )
        mod = self._module("profile:work")
        self.assertTrue(ps.install(mod))
        self.assertEqual(mod._resolve_container_task_id("s"), "profile:work.gamma")

    def test_profile_with_no_active_project_does_not_leak_defaults(self):
        """Regression for the reported bug: a fresh profile session mounted the
        default profile's folder at /workspace.

        The default home has an active project; profile 'work' has projects but
        none active and no recorded cwd. Resolving base 'profile:work' must
        yield no project (scope stays 'profile:work') instead of guessing with
        the default profile's active_id.
        """
        root = self.home
        make_dbs(root, [("p1", "alpha", self.alpha)], [], active_id="p1")
        work_home = os.path.join(root, "profiles", "work")
        os.makedirs(work_home, exist_ok=True)
        make_dbs(work_home, [("w1", "gamma", os.path.join(work_home, "Gamma"))], [])
        mod = self._module("profile:work")
        self.assertTrue(ps.install(mod))
        self.assertEqual(mod._resolve_container_task_id("no-such-session"), "profile:work")

    def test_leaves_session_key_alone(self):
        '"session:abc" is per-session isolation — must not be scoped.'
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        mod = self._module("session:abc")
        ps.install(mod)
        self.assertEqual(mod._resolve_container_task_id("s"), "session:abc")

    def test_leaves_shared_key_alone(self):
        '"shared:team" is explicit opt-in sharing — must not be scoped.'
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        mod = self._module("shared:team")
        ps.install(mod)
        self.assertEqual(mod._resolve_container_task_id("s"), "shared:team")

    def test_install_is_idempotent(self):
        mod = self._module("default")
        self.assertTrue(ps.install(mod))
        first = mod._resolve_container_task_id
        self.assertTrue(ps.install(mod))
        self.assertIs(mod._resolve_container_task_id, first)

    def test_missing_function_is_reported_not_raised(self):
        import types

        self.assertFalse(ps.install(types.ModuleType("empty")))

    def test_failure_falls_back_to_original(self):
        """A broken DB must degrade to stock behaviour, not break the terminal."""
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        mod = self._module("default")
        ps.install(mod)
        with open(os.path.join(self.home, "projects.db"), "w") as f:
            f.write("not a database")
        ps._cache.clear()
        ps._folders_cache.clear()
        self.assertEqual(mod._resolve_container_task_id("s"), "default")


class TestSessionIdFallback(ScopeTestBase):
    """task_id may be None for the top-level agent; session_id must carry it."""

    def tearDown(self):
        ps.clear_ids(None)
        super().tearDown()

    def test_resolves_via_session_id_when_task_id_is_none(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-b", self.beta)],
            active_id="p1",
        )
        ps.note_ids(None, "sess-b")
        slug, source = ps.resolve_project_slug(None)
        self.assertEqual(slug, "beta")
        self.assertEqual(source, "session")

    def test_task_id_takes_precedence(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-a", self.alpha), ("sess-b", self.beta)],
        )
        ps.note_ids("sess-a", "sess-b")
        slug, _ = ps.resolve_project_slug("sess-a")
        self.assertEqual(slug, "alpha")

    def test_threads_do_not_leak_ids(self):
        """Concurrency guard: one thread's session must not bleed into another."""
        import threading

        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("sess-a", self.alpha), ("sess-b", self.beta)],
        )
        results = {}
        barrier = threading.Barrier(2)

        def run(name, sid):
            ps.note_ids(None, sid)
            barrier.wait(timeout=5)  # force overlap
            results[name] = ps.resolve_project_slug(None)[0]

        threads = [
            threading.Thread(target=run, args=("a", "sess-a")),
            threading.Thread(target=run, args=("b", "sess-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(results.get("a"), "alpha")
        self.assertEqual(results.get("b"), "beta")

    def test_entry_capture_wraps_and_restores(self):
        import types

        seen = {}

        def fake_terminal_tool(**kwargs):
            seen["ids"] = ps.current_ids()
            return "ok"

        mod = types.ModuleType("fake")
        mod._resolve_container_task_id = lambda task_id=None: "default"
        mod.terminal_tool = fake_terminal_tool
        ps.install(mod)

        self.assertEqual(mod.terminal_tool(task_id="t1", session_id="s1"), "ok")
        self.assertEqual(seen["ids"], ("t1", "s1"))
        # Restored after the call so ids never leak between tool invocations.
        self.assertIsNone(ps.current_ids())


class TestFallbackCaching(ScopeTestBase):
    """A guess must not be cached as long as a real answer.

    A new session's state.db row is written asynchronously, so its first tool
    call can fall back to the global active_id. Caching that for the full TTL
    pinned the wrong project long enough to create a container and run the
    first command in another project's workspace.
    """

    def test_fallback_is_rechecked_once_the_session_row_lands(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [],  # no session row yet
            active_id="p1",
        )
        slug, source = ps.resolve_project_slug("late-session")
        self.assertEqual((slug, source), ("alpha", "active"))

        # Row arrives (as the gateway writes it a moment later).
        conn = sqlite3.connect(os.path.join(self.home, "state.db"))
        conn.execute(
            "INSERT INTO sessions (id, session_key, cwd) VALUES ('late-session','',?)",
            (self.beta,),
        )
        conn.commit()
        conn.close()

        time.sleep(ps._FALLBACK_CACHE_TTL + 0.05)
        slug, source = ps.resolve_project_slug("late-session")
        self.assertEqual((slug, source), ("beta", "session"))

    def test_session_results_use_the_long_ttl(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.alpha)], "p1")
        self.assertEqual(ps.resolve_project_slug("s"), ("alpha", "session"))
        # Still served from cache after the short fallback TTL has passed.
        time.sleep(ps._FALLBACK_CACHE_TTL + 0.05)
        self.assertEqual(ps.resolve_project_slug("s"), ("alpha", "session"))


class TestCacheBounds(ScopeTestBase):
    """The cache is keyed per session, so it must not grow without bound."""

    def test_cache_is_bounded(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [], "p1")
        for i in range(ps._CACHE_MAX * 3):
            ps.resolve_project_slug("session-%d" % i)
        self.assertLessEqual(len(ps._cache), ps._CACHE_MAX)

    def test_live_entries_survive_pruning(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("keep", self.alpha)], "p1")
        self.assertEqual(ps.resolve_project_slug("keep")[1], "session")
        for i in range(ps._CACHE_MAX * 2):
            ps.resolve_project_slug("filler-%d" % i)
        # Still resolves correctly whether or not it survived eviction.
        self.assertEqual(ps.resolve_project_slug("keep"), ("alpha", "session"))


class TestFolderCaching(ScopeTestBase):
    def test_folder_cache_invalidates_when_projects_change(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [("s", self.beta)], "p1")
        self.assertIsNone(ps._project_for_path(self.beta))

        conn = sqlite3.connect(os.path.join(self.home, "projects.db"))
        conn.execute(
            "INSERT INTO projects (id, slug, name, primary_path) VALUES ('p2','beta','beta',?)",
            (self.beta,),
        )
        conn.commit()
        conn.close()
        ps._folders_cache.clear()
        self.assertEqual(ps._project_for_path(self.beta), "beta")

    def test_symlinked_path_still_matches(self):
        """A cwd reached through a symlink must resolve to its project."""
        link = os.path.join(self.home, "link-to-beta")
        os.symlink(self.beta, link)
        make_dbs(self.home, [("p2", "beta", self.beta)], [("s", link)], "p2")
        self.assertEqual(ps._project_for_path(link), "beta")


class TestStickyBinding(ScopeTestBase):
    """A session must stay on one project once it has resolved.

    Hermes writes sessions.cwd only after a terminal command settles, so a new
    session's first call falls back to the global active_id. Without a binding
    every later call re-reads that global value, so switching projects in the
    UI would move a running session onto another project's container.
    """

    def setUp(self):
        super().setUp()
        ps._bindings.clear()

    def tearDown(self):
        ps._bindings.clear()
        super().tearDown()

    def _set_active(self, project_id):
        conn = sqlite3.connect(os.path.join(self.home, "projects.db"))
        conn.execute("INSERT OR REPLACE INTO project_meta (key, value) VALUES ('active_id', ?)",
                     (project_id,))
        conn.commit(); conn.close()
        ps._cache.clear()

    def test_session_sticks_when_active_project_changes(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [],  # no cwd recorded yet — the first-call situation
            active_id="p1",
        )
        first = ps.resolve_project_slug("new-session")
        self.assertEqual(first, ("alpha", "active"))

        self._set_active("p2")  # user switches project in the UI
        second = ps.resolve_project_slug("new-session")
        self.assertEqual(second[0], "alpha", "session moved to another project's container")
        self.assertEqual(second[1], "bound")

    def test_recorded_cwd_overrides_a_wrong_binding(self):
        """A session that had to guess corrects itself once cwd is written."""
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [],
            active_id="p1",
        )
        self.assertEqual(ps.resolve_project_slug("late")[0], "alpha")

        conn = sqlite3.connect(os.path.join(self.home, "state.db"))
        conn.execute("INSERT INTO sessions (id, session_key, cwd) VALUES ('late','',?)",
                     (self.beta,))
        conn.commit(); conn.close()
        ps._cache.clear()

        slug, source = ps.resolve_project_slug("late")
        self.assertEqual((slug, source), ("beta", "session"))

    def test_bindings_are_bounded(self):
        make_dbs(self.home, [("p1", "alpha", self.alpha)], [], "p1")
        for i in range(ps._BINDINGS_MAX * 2):
            ps.resolve_project_slug("s-%d" % i)
        self.assertLessEqual(len(ps._bindings), ps._BINDINGS_MAX)

    def test_distinct_sessions_keep_distinct_bindings(self):
        make_dbs(
            self.home,
            [("p1", "alpha", self.alpha), ("p2", "beta", self.beta)],
            [("has-cwd", self.beta)],
            active_id="p1",
        )
        self.assertEqual(ps.resolve_project_slug("has-cwd")[0], "beta")
        self.assertEqual(ps.resolve_project_slug("no-cwd")[0], "alpha")
        self.assertEqual(ps.resolve_project_slug("has-cwd")[0], "beta")


class TestProjectPath(ScopeTestBase):
    def test_returns_primary_path(self):
        make_dbs(self.home, [("p1", "beta", self.beta)], [("s", self.beta)], "p1")
        self.assertEqual(ps.project_path_for_task("s"), self.beta)

    def test_none_when_unresolvable(self):
        make_dbs(self.home, [], [])
        self.assertIsNone(ps.project_path_for_task("s"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
