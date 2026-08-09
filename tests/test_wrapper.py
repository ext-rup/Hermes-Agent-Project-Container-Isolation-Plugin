"""Tests for the Hermes docker-to-Apple-Container shim.

Focus is the behaviour Hermes' container reuse actually depends on:
the ps/label round-trip, the Go-template subset, and the run-arg rewriting.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest

WRAPPER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "hermes-projects-apple", "docker-wrapper",
)
spec = importlib.util.spec_from_loader(
    "docker_wrapper", importlib.machinery.SourceFileLoader("docker_wrapper", WRAPPER_PATH)
)
dw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dw)


def make_projects_db(path, projects, active_id=None):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE project_folders (
            project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, path));
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    for pid, slug, name, ppath in projects:
        conn.execute(
            "INSERT INTO projects (id, slug, name, primary_path, created_at) VALUES (?,?,?,?,0)",
            (pid, slug, name, ppath),
        )
    if active_id:
        conn.execute("INSERT INTO project_meta (key, value) VALUES ('active_id', ?)", (active_id,))
    conn.commit()
    conn.close()


class TestScoping(unittest.TestCase):
    def test_scope_task_id(self):
        self.assertEqual(dw.scope_task_id("default", "alpha"), "default.alpha")

    def test_scope_is_idempotent(self):
        """A label read back out of `ps` must not get double-scoped."""
        once = dw.scope_task_id("default", "alpha")
        self.assertEqual(dw.scope_task_id(once, "alpha"), once)

    def test_scope_without_project_is_noop(self):
        self.assertEqual(dw.scope_task_id("default", None), "default")

    def test_scoped_id_stays_label_safe(self):
        """Hermes sanitizes labels to [A-Za-z0-9_.-]; ours must match."""
        scoped = dw.scope_task_id("task/one", "my project")
        self.assertRegex(scoped, r"^[A-Za-z0-9_.-]+$")

    def test_unscope_roundtrip(self):
        self.assertEqual(dw.unscope_task_id("default.alpha", "alpha"), "default")

    def test_backend_probe_is_never_scoped(self):
        """One shared probe container, not one per project."""
        self.assertEqual(
            dw.scope_task_id("prompt-backend-probe", "alpha"), "prompt-backend-probe"
        )
        self.assertEqual(
            dw.scope_task_id("prompt-backend-probe", "beta"), "prompt-backend-probe"
        )


class TestActiveProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "projects.db")
        os.environ.pop("HERMES_PROJECT_SLUG", None)

    def tearDown(self):
        os.environ.pop("HERMES_PROJECT_SLUG", None)

    def test_reads_active_project(self):
        make_projects_db(
            self.db,
            [("p_1", "alpha", "Alpha", "/tmp/alpha"),
             ("p_2", "beta", "Beta", "/tmp/beta")],
            active_id="p_2",
        )
        self.assertEqual(dw.active_project(self.db), ("beta", "/tmp/beta"))

    def test_no_active_id_returns_none(self):
        make_projects_db(self.db, [("p_1", "alpha", "Alpha", "/tmp/alpha")])
        self.assertEqual(dw.active_project(self.db), (None, None))

    def test_missing_db_returns_none(self):
        self.assertEqual(dw.active_project(os.path.join(self.tmp, "nope.db")), (None, None))

    def test_env_override_wins(self):
        make_projects_db(
            self.db,
            [("p_1", "alpha", "Alpha", "/tmp/alpha"),
             ("p_2", "beta", "Beta", "/tmp/beta")],
            active_id="p_2",
        )
        os.environ["HERMES_PROJECT_SLUG"] = "alpha"
        self.assertEqual(dw.active_project(self.db), ("alpha", "/tmp/alpha"))

    def test_falls_back_to_primary_folder(self):
        make_projects_db(self.db, [("p_1", "alpha", "Alpha", None)], active_id="p_1")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO project_folders (project_id, path, is_primary) VALUES ('p_1','/tmp/from-folder',1)"
        )
        conn.commit()
        conn.close()
        self.assertEqual(dw.active_project(self.db), ("alpha", "/tmp/from-folder"))


class TestTemplates(unittest.TestCase):
    def test_hermes_reuse_probe_template(self):
        """The exact template from _find_reusable_container."""
        rec = {"ID": "hermes-abc123", "State": "running", "Labels": {}}
        self.assertEqual(
            dw.render_template("{{.ID}}\t{{.State}}", rec), "hermes-abc123\trunning"
        )

    def test_label_template(self):
        rec = {"ID": "x", "State": "running", "Labels": {"hermes-egress": "off"}}
        out = dw.render_template('{{.ID}}\t{{.State}}\t{{.Label "hermes-egress"}}', rec)
        self.assertEqual(out, "x\trunning\toff")

    def test_missing_label_renders_no_value(self):
        """Hermes checks `egress_val not in ("", "<no value>", "off")`."""
        rec = {"ID": "x", "State": "running", "Labels": {}}
        out = dw.render_template('{{.Label "hermes-egress"}}', rec)
        self.assertEqual(out, "<no value>")

    def test_nested_field(self):
        view = {"State": {"FinishedAt": "2026-01-01T00:00:00Z"}}
        self.assertEqual(
            dw.render_template("{{.State.FinishedAt}}", view), "2026-01-01T00:00:00Z"
        )

    def test_network_mode_template(self):
        view = {"HostConfig": {"NetworkMode": "default"}}
        self.assertEqual(dw.render_template("{{.HostConfig.NetworkMode}}", view), "default")

    def test_json_template(self):
        view = {"Config": {"Entrypoint": ["/init"]}}
        self.assertEqual(
            dw.render_template("{{json .Config.Entrypoint}}", view), '["/init"]'
        )

    def test_json_template_null(self):
        view = {"Config": {"Entrypoint": None}}
        self.assertEqual(dw.render_template("{{json .Config.Entrypoint}}", view), "null")


class TestPsFiltering(unittest.TestCase):
    def records(self):
        return [
            {"ID": "c1", "Names": "c1", "Image": "node", "State": "running", "CreatedAt": "",
             "Labels": {"hermes-agent": "1", "hermes-task-id": "default.alpha"}},
            {"ID": "c2", "Names": "c2", "Image": "node", "State": "running", "CreatedAt": "",
             "Labels": {"hermes-agent": "1", "hermes-task-id": "default.beta"}},
            {"ID": "c3", "Names": "c3", "Image": "node", "State": "exited", "CreatedAt": "",
             "Labels": {"hermes-agent": "1", "hermes-task-id": "default.alpha"}},
        ]

    def test_filter_scopes_task_id_query(self):
        """Hermes asks for task-id=default; it must only see its project."""
        args = ["-a", "--filter", "label=hermes-agent=1",
                "--filter", "label=hermes-task-id=default",
                "--format", "{{.ID}}\t{{.State}}"]
        show_all, filters, fmt, quiet = dw.parse_ps_args(args, "alpha")
        got = dw.filter_records(self.records(), filters, show_all)
        self.assertEqual([r["ID"] for r in got], ["c1", "c3"])

    def test_other_project_is_invisible(self):
        args = ["-a", "--filter", "label=hermes-task-id=default"]
        show_all, filters, _, _ = dw.parse_ps_args(args, "beta")
        got = dw.filter_records(self.records(), filters, show_all)
        self.assertEqual([r["ID"] for r in got], ["c2"])

    def test_without_all_only_running(self):
        args = ["--filter", "label=hermes-task-id=default"]
        show_all, filters, _, _ = dw.parse_ps_args(args, "alpha")
        got = dw.filter_records(self.records(), filters, show_all)
        self.assertEqual([r["ID"] for r in got], ["c1"])

    def test_unscoped_when_no_project(self):
        args = ["-a", "--filter", "label=hermes-task-id=default.alpha"]
        show_all, filters, _, _ = dw.parse_ps_args(args, None)
        got = dw.filter_records(self.records(), filters, show_all)
        self.assertEqual([r["ID"] for r in got], ["c1", "c3"])

    def test_equals_form_filter(self):
        args = ["-a", "--filter=label=hermes-task-id=default"]
        show_all, filters, _, _ = dw.parse_ps_args(args, "alpha")
        got = dw.filter_records(self.records(), filters, show_all)
        self.assertEqual([r["ID"] for r in got], ["c1", "c3"])




class TestAlreadyScoped(unittest.TestCase):
    """Regression: the plugin and the shim must not both scope a task id.

    The plugin resolves per session; the shim can only see the global active
    project. When they disagreed the label became "default.alpha.beta" and
    a Alpha session was routed to a Beta container.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "projects.db")
        make_projects_db(
            self.db,
            [("p1", "alpha", "Alpha", "/tmp/alpha"),
             ("p2", "beta", "Beta", "/tmp/beta")],
            active_id="p2",
        )
        self._orig_db = dw.PROJECTS_DB
        dw.PROJECTS_DB = self.db

    def tearDown(self):
        dw.PROJECTS_DB = self._orig_db

    def test_split_scoped_recognises_known_slug(self):
        self.assertEqual(dw.split_scoped("default.alpha"), ("default", "alpha"))

    def test_split_scoped_ignores_unknown_suffix(self):
        self.assertEqual(dw.split_scoped("default.nope"), ("default.nope", None))

    def test_split_scoped_handles_unscoped(self):
        self.assertEqual(dw.split_scoped("default"), ("default", None))

    def test_run_uses_label_project_not_active(self):
        """Active project is beta; the label says alpha — label must win."""
        args = ["--label", "hermes-task-id=default.alpha", "-v", "/x:/workspace"]
        slug, path, already = dw._project_for_run(args, dict(dw.DEFAULT_CONFIG))
        self.assertEqual(slug, "alpha")
        self.assertTrue(already)

    def test_label_is_not_rescoped(self):
        args = ["--label", "hermes-task-id=default.alpha", "-v", "/x:/workspace"]
        out, _ = dw.rewrite_run_args(
            args, "alpha", "/tmp/alpha", dict(dw.DEFAULT_CONFIG), already_scoped=True
        )
        self.assertIn("hermes-task-id=default.alpha", out)
        self.assertNotIn("hermes-task-id=default.alpha.alpha", out)
        self.assertNotIn("hermes-task-id=default.alpha.beta", " ".join(out))

    def test_falls_back_to_active_without_plugin(self):
        """No project in the label — shim scopes it itself."""
        args = ["--label", "hermes-task-id=default", "-v", "/x:/workspace"]
        slug, _, already = dw._project_for_run(args, dict(dw.DEFAULT_CONFIG))
        self.assertEqual(slug, "beta")
        self.assertFalse(already)

    def test_ps_filter_not_rescoped_when_already_scoped(self):
        filters = {}
        dw._add_filter(filters, "label=hermes-task-id=default.alpha", "beta")
        self.assertEqual(filters["_labels"]["hermes-task-id"], "default.alpha")

    def test_ps_filter_scoped_when_bare(self):
        filters = {}
        dw._add_filter(filters, "label=hermes-task-id=default", "beta")
        self.assertEqual(filters["_labels"]["hermes-task-id"], "default.beta")


class TestVolumeSplitting(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(dw.split_volume("/a:/workspace"), ("/a", "/workspace", None))

    def test_with_options(self):
        self.assertEqual(
            dw.split_volume("/a:/root/.hermes/skills:ro"),
            ("/a", "/root/.hermes/skills", "ro"),
        )

    def test_path_with_spaces(self):
        """The real config has "/Users/you/.../Subproject:/workspace/Subproject"."""
        src, dst, opts = dw.split_volume(
            "/Users/you/Projects/alpha/subproject:/workspace/Subproject"
        )
        self.assertEqual(src, "/Users/you/Projects/alpha/subproject")
        self.assertEqual(dst, "/workspace/Subproject")
        self.assertIsNone(opts)


class TestRunRewriting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = os.path.join(self.tmp, "Alpha")
        os.makedirs(self.project)
        self.config = dict(dw.DEFAULT_CONFIG)

    def test_scopes_task_label(self):
        args = ["-d", "--label", "hermes-task-id=default", "--label", "hermes-agent=1"]
        out, notes = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn("hermes-task-id=default.alpha", out)
        self.assertIn("hermes-agent=1", out)

    def test_repoints_workspace_mount(self):
        args = ["-v", "/somewhere/else:/workspace"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn(f"{self.project}:/workspace", out)

    def test_leaves_workspace_alone_when_already_correct(self):
        args = ["-v", f"{self.project}:/workspace"]
        out, notes = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn(f"{self.project}:/workspace", out)
        self.assertEqual(notes, [])

    def test_drops_foreign_subworkspace_mount(self):
        args = ["-v", "/other/project/Sub:/workspace/Sub"]
        out, notes = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertNotIn("/other/project/Sub:/workspace/Sub", out)
        self.assertTrue(any("dropped" in n for n in notes))

    def test_keeps_subworkspace_mount_inside_project(self):
        inner = os.path.join(self.project, "Sub")
        args = ["-v", f"{inner}:/workspace/Sub"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn(f"{inner}:/workspace/Sub", out)

    def test_preserves_readonly_mounts(self):
        args = ["-v", "/Users/you/.hermes/skills:/root/.hermes/skills:ro"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn("/Users/you/.hermes/skills:/root/.hermes/skills:ro", out)

    def test_no_project_leaves_args_untouched(self):
        args = ["-v", "/a:/workspace", "--label", "hermes-task-id=default"]
        out, notes = dw.rewrite_run_args(args, None, None, self.config)
        self.assertEqual(out, args)
        self.assertEqual(notes, [])

    def test_scopes_sandbox_home(self):
        home = os.path.join(dw.SANDBOX_ROOT, "default", "home")
        args = ["-v", f"{home}:/root", "--label", "hermes-task-id=default"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        joined = " ".join(out)
        self.assertIn("default.alpha", joined)
        self.assertNotIn(f"{home}:/root", joined)

    def test_probe_label_and_name_left_alone(self):
        """The probe keeps one identity across projects, so it is reused."""
        args = ["--label", "hermes-task-id=prompt-backend-probe",
                "--name", "hermes-abc123", "-v", "/a:/workspace"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertIn("hermes-task-id=prompt-backend-probe", out)
        self.assertIn("hermes-abc123", out)
        self.assertNotIn("hermes-abc123-alpha", out)

    def test_probe_gets_no_project_mount(self):
        """A container shared by every project must not hold one project's files."""
        args = ["--label", "hermes-task-id=prompt-backend-probe",
                "-v", "/sandbox/workspace:/workspace"]
        slug, path, already = dw._project_for_run(args, dict(dw.DEFAULT_CONFIG))
        self.assertIsNone(slug)
        self.assertFalse(already)

    def test_image_and_command_survive(self):
        args = ["-d", "--label", "hermes-task-id=default", "-v", "/a:/workspace",
                "node:24-bookworm-slim", "sleep", "infinity"]
        out, _ = dw.rewrite_run_args(args, "alpha", self.project, self.config)
        self.assertEqual(out[-3:], ["node:24-bookworm-slim", "sleep", "infinity"])


class TestTranslate(unittest.TestCase):
    def test_exec_keeps_interactive_and_tty(self):
        """Apple Container's exec does support -i/-t."""
        out = dw.translate_args(["-i", "-t"], subcommand="exec")
        self.assertEqual(out, ["--interactive", "--tty"])

    def test_build_t_is_tag(self):
        out = dw.translate_args(["-t", "myimage"], subcommand="build")
        self.assertEqual(out, ["--tag", "myimage"])

    def test_run_t_is_tty(self):
        out = dw.translate_args(["-t"], subcommand="run")
        self.assertEqual(out, ["--tty"])

    def test_strips_unsupported_with_value(self):
        out = dw.translate_args(["--pids-limit", "512", "-d"], subcommand="run")
        self.assertEqual(out, ["--detach"])

    def test_strips_privileged(self):
        self.assertEqual(dw.translate_args(["--privileged", "-d"]), ["--detach"])

    def test_cpus_coerced_to_int(self):
        self.assertEqual(dw.translate_args(["--cpus", "1.5"]), ["--cpus", "1"])

    def test_preserves_labels(self):
        out = dw.translate_args(["--label", "hermes-task-id=default.alpha"])
        self.assertEqual(out, ["--label", "hermes-task-id=default.alpha"])

    def test_tmpfs_and_shm_pass_through(self):
        out = dw.translate_args(["--tmpfs", "/workspace:rw,exec,size=10g", "--shm-size", "1g"])
        self.assertEqual(out, ["--tmpfs", "/workspace:rw,exec,size=10g", "--shm-size", "1g"])


class TestStateMapping(unittest.TestCase):
    def test_stopped_maps_to_exited(self):
        """Hermes treats anything != running as needing `docker start`."""
        self.assertEqual(dw._STATE_MAP["stopped"], "exited")
        self.assertEqual(dw._STATE_MAP["running"], "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
