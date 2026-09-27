import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage", ROOT / "scripts/manage.py")
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class ManagementTests(unittest.TestCase):
    def test_manifest_and_config(self):
        manifest = manage.load_manifest()
        self.assertEqual(5, len(manifest["agents"]))
        defaults = json.loads((ROOT / "config/defaults.json").read_text())
        self.assertEqual("comment", defaults["publish_mode"])
        manage.validate_defaults(defaults)
        broken = dict(defaults, workspace_root="relative/path")
        with self.assertRaises(manage.ManagementError):
            manage.validate_defaults(broken)

    def test_actual_profile_and_workflow_validation(self):
        # Exercises CAO's installed profile schema and script linter.
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CAO_HOME_DIR": directory}):
            self.assertEqual(1, len(manage.validate()["workflows"]))

    def test_install_skips_identical_owned_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest = manage.load_manifest()
            chosen = manage.selected(manifest, "github-pr-review")
            state = manage.load_state(home)
            for kind in ("agents", "workflows"):
                for entry in chosen[kind]:
                    dst = manage.target(home, kind, entry)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes((ROOT / entry["source"]).read_bytes())
                    if kind == "agents":
                        context = manage.profile_context(home, entry["name"])
                        context.parent.mkdir(parents=True, exist_ok=True)
                        context.write_bytes(dst.read_bytes())
                    state[kind][entry["name"]] = {"sha256": manage.digest(dst)}
            manage.atomic_json(manage.state_path(home), state)
            with patch.object(manage, "validate", return_value=manifest), patch.object(manage, "cao_home", return_value=home), patch.object(manage, "command") as command:
                manage.install(None)
                command.assert_not_called()

    def test_uninstall_never_removes_unowned_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manifest = manage.load_manifest()
            unowned = manage.target(home, "agents", manifest["agents"][0])
            unowned.parent.mkdir(parents=True)
            unowned.write_text("someone else's profile")
            with patch.object(manage, "cao_home", return_value=home), patch.object(manage, "command") as command:
                manage.uninstall(None, True)
                command.assert_not_called()
            self.assertTrue(unowned.exists())

    def test_modified_owned_resource_blocks_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            entry = manage.load_manifest()["agents"][0]
            dst = manage.target(home, "agents", entry)
            dst.parent.mkdir(parents=True)
            dst.write_text("modified")
            state = manage.load_state(home)
            state["agents"][entry["name"]] = {"sha256": "old"}
            manage.atomic_json(manage.state_path(home), state)
            with patch.object(manage, "cao_home", return_value=home):
                with self.assertRaises(manage.ManagementError):
                    manage.uninstall(None, True)
            self.assertTrue(dst.exists())

    def test_modified_context_blocks_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            entry = manage.load_manifest()["agents"][0]
            dst = manage.target(home, "agents", entry)
            context = manage.profile_context(home, entry["name"])
            dst.parent.mkdir(parents=True)
            context.parent.mkdir(parents=True)
            dst.write_bytes((ROOT / entry["source"]).read_bytes())
            context.write_text("someone else's context")
            state = manage.load_state(home)
            state["agents"][entry["name"]] = {"sha256": manage.digest(dst)}
            manage.atomic_json(manage.state_path(home), state)
            with patch.object(manage, "cao_home", return_value=home):
                with self.assertRaises(manage.ManagementError):
                    manage.uninstall(None, True)
            self.assertTrue(context.exists())

    def test_run_wrapper_maps_inputs_to_cao(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "codex/cao_pr_review_readonly.config.toml"
            config.parent.mkdir()
            config.write_text('sandbox_mode = "read-only"\napproval_policy = "never"\n')
            entry = manage.load_manifest()["workflows"][0]
            dst = manage.target(home, "workflows", entry)
            dst.parent.mkdir()
            dst.write_bytes((ROOT / entry["source"]).read_bytes())
            state = manage.load_state(home)
            state["workflows"][entry["name"]] = {"sha256": manage.digest(dst)}
            manage.atomic_json(manage.state_path(home), state)
            with patch.dict("os.environ", {"CODEX_HOME": str(config.parent)}), patch.object(manage, "cao_home", return_value=home), patch.object(manage, "prerequisites"), patch.object(manage.subprocess, "call", return_value=0) as call:
                with self.assertRaises(SystemExit) as result:
                    manage.run(["github-pr-review", "--repository", "owner/repo", "--pr", "312", "--dry-run"])
            self.assertEqual(0, result.exception.code)
            argv = call.call_args.args[0]
            self.assertEqual(str(dst), argv[3])
            self.assertIn("pr_number=312", argv)
            self.assertIn("publish_mode=dry-run", argv)


if __name__ == "__main__":
    unittest.main()
