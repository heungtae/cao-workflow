import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
shim = types.ModuleType("cao_workflow")
shim.emit_output = lambda value: value
shim.get_inputs = lambda: {}
shim.step = lambda *a, **kw: None
sys.modules.setdefault("cao_workflow", shim)
spec = importlib.util.spec_from_file_location("pr_workflow", ROOT / "workflows/github-pr-review/workflow.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((FIX / name).read_text())

    def test_discovery_filters_drafts_and_base(self):
        simple = self.fixture("pr/simple.json")
        draft = self.fixture("pr/draft.json")
        with patch.object(workflow, "pages", return_value=[simple, draft]):
            self.assertEqual([312], [p["number"] for p in workflow.discover("owner/repo", None, "main", False)])
            self.assertEqual(2, len(workflow.discover("owner/repo", None, None, True)))

    def test_marker_changes_with_sha_and_detects_existing(self):
        old = self.fixture("pr/simple.json")
        new = self.fixture("pr/updated-sha.json")
        a = workflow.marker("owner/repo", 312, old["head"]["sha"])
        b = workflow.marker("owner/repo", 312, new["head"]["sha"])
        self.assertNotEqual(a, b)
        self.assertTrue(workflow.has_marker([{"body": "review\n" + a}], a))
        self.assertFalse(workflow.has_marker([{"body": "review\n" + a}], b))

    def test_same_sha_skips_before_checkout(self):
        pr = self.fixture("pr/simple.json")
        identity = workflow.marker("owner/repo", 312, pr["head"]["sha"])
        identity += f"\n<!-- cao-review-base {pr['base']['sha']} -->"
        with patch.object(workflow, "pages", side_effect=[[{"body": identity}], []]), patch.object(workflow, "checkout") as checkout:
            result = workflow.process_pr("owner/repo", pr, {"publish_mode": "review"})
        self.assertEqual("skipped", result["result"])
        checkout.assert_not_called()

    def test_checkout_rejects_shared_or_symlink_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "review-root"
            root.mkdir(mode=0o700)
            root.chmod(0o775)
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                workflow.checkout("owner/repo", 1, "head", str(root))
            link = Path(temporary) / "review-link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                workflow.checkout("owner/repo", 1, "head", str(link))

    def test_agent_invocations_use_fixed_parent_of_checkout(self):
        work = Path("/tmp/cao-pr-review/owner-repo-pr-1-unique")
        reviewer = {"findings": [], "summary": "ok"}
        with patch.object(workflow, "codex_json", return_value=reviewer) as run_codex:
            workflow.review_chunk(0, "patch", {}, work)
            self.assertEqual(3, len(run_codex.call_args_list))
            self.assertTrue(all(call.args[3] == work for call in run_codex.call_args_list))
        finding = self.fixture("reviews/code-review.json")["findings"][0]
        with patch.object(workflow, "codex_json", return_value=reviewer) as run_codex:
            with self.assertRaisesRegex(RuntimeError, "removed all reviewer findings"):
                workflow.aggregate([{"findings": [finding]}], {finding["file"]: {finding["line"]}}, 1, work, [])
            self.assertEqual(work, run_codex.call_args.args[3])
        with patch.object(workflow, "codex_json", return_value={"publish": True, "reason": ""}) as run_codex:
            workflow.publication_gate({"findings": []}, "review", [], 1, work)
            self.assertEqual(work, run_codex.call_args.args[3])

    def test_cao_step_receives_only_shell_inert_carrier_and_cleans_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "pr"
            work.mkdir()
            def complete(provider, role, carrier, **kwargs):
                self.assertEqual("codex", provider)
                self.assertEqual("pr-code-reviewer", role)
                self.assertRegex(carrier, r"^: CAO_REVIEW_INPUT_[0-9a-f]{32}$")
                token = carrier.split()[1]
                self.assertEqual({"task": "untrusted prompt"}, json.loads((root / "inputs" / f"{token}.json").read_text()))
                self.assertEqual(str(root), kwargs["working_directory"])
                self.assertEqual("manual", kwargs["recovery"])
                return types.SimpleNamespace(output='• {"findings":[],"summary":"ok"}\n\n• WebSocket timing: 12ms')
            with patch.object(workflow, "step", side_effect=complete) as run_step:
                result = workflow.codex_json("pr-code-reviewer", "untrusted prompt", "chunk-0-pr-code-reviewer", work)
            self.assertEqual([], result["findings"])
            self.assertEqual(1, run_step.call_count)
            self.assertEqual([], list((root / "inputs").iterdir()))
            with patch.object(workflow, "step", return_value=types.SimpleNamespace(output="bash: CAO_REVIEW_INPUT: command not found")):
                with self.assertRaises(ValueError):
                    workflow.codex_json("pr-code-reviewer", "prompt", "failed", work)
            self.assertEqual([], list((root / "inputs").iterdir()))

    def test_invalid_response_retries_with_distinct_invocation(self):
        responses = [{"findings": "invalid"}, {"findings": [], "summary": "ok"}]
        with patch.object(workflow, "codex_json", side_effect=responses) as run_codex:
            result = workflow.structured_review("pr-code-reviewer", "prompt", "chunk-0-pr-code-reviewer", {}, Path("/tmp/cao-pr-review/pr"), None)
        self.assertEqual([], result["findings"])
        self.assertEqual(["chunk-0-pr-code-reviewer", "chunk-0-pr-code-reviewer-retry-1"],
                         [call.args[2] for call in run_codex.call_args_list])

    def test_cao_terminal_footer_after_json_is_ignored(self):
        output = ('• Explored\n  └ Read CAO_REVIEW_INPUT_abc.json\n\n'
                  '• {"findings":[],\n  "summary":"two\n  words"}\n\n'
                  '  9:59 PM · WebSocket: 7 events send (1ms) • 282 events received')
        self.assertEqual({"findings": [], "summary": "two words"}, workflow.response_object(output))

    def test_context_chunk_and_validation(self):
        patch_text = "@@ -10,2 +10,3 @@\n context\n+new\n-old\n+another\n"
        self.assertEqual({11, 12}, workflow.changed_lines(patch_text))
        original_limit = workflow.MAX_PATCH
        try:
            workflow.MAX_PATCH = 120
            large_patch = "@@ -1,1 +1,30 @@\n" + "+line\n" * 30
            segments = workflow.patch_segments(large_patch)
            self.assertGreater(len(segments), 1)
            self.assertEqual(workflow.changed_lines(large_patch), set().union(*(workflow.changed_lines(item) for item in segments)))
        finally:
            workflow.MAX_PATCH = original_limit
        data = self.fixture("reviews/code-review.json")
        result = workflow.parse_result(json.dumps(data), {"src/consumer.py": {12}})
        self.assertEqual(1, len(result["findings"]))
        self.assertEqual([], workflow.parse_result(json.dumps(data), {"src/consumer.py": {13}})["findings"])
        batches = workflow.chunks([{"filename": "src/consumer.py", "patch": patch_text, "lines": {11}}], "metadata")
        self.assertEqual(1, len(batches))
        self.assertIn("src/consumer.py", batches[0][0])
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", workflow.safe_text("token=ghp_abcdefghijklmnopqrstuvwxyz", 200))

    def test_aggregation_deduplicates_and_gate_never_approves(self):
        code = self.fixture("reviews/code-review.json")
        test = self.fixture("reviews/test-review.json")
        duplicates = [{"findings": code["findings"]}, {"findings": code["findings"] + test["findings"]}]
        fake = {"findings": code["findings"] + code["findings"] + test["findings"], "summary": "Review"}
        with patch.object(workflow, "codex_json", return_value=fake):
            merged = workflow.aggregate(duplicates, {"src/consumer.py": {12}}, 312, Path("/tmp/pr"), [])
        self.assertEqual(2, len(merged["findings"]))
        body = workflow.render_review(merged, "81ac79e", workflow.marker("owner/repo", 312, "81ac79e"))
        inline = workflow.render_inline_comments(merged, {"src/consumer.py": {12}})
        self.assertNotIn("Offset committed early", body)
        self.assertIn("Offset committed early", inline[0]["body"])
        self.assertEqual({"path": "src/consumer.py", "line": 12, "side": "RIGHT"},
                         {key: inline[0][key] for key in ("path", "line", "side")})
        with patch.object(workflow, "codex_json", return_value={"publish": True, "reason": ""}):
            workflow.publication_gate(merged, body, inline, 312, Path("/tmp"))
        with patch.object(workflow, "codex_json", return_value={"publish": False, "reason": "mismatch"}):
            with self.assertRaises(RuntimeError):
                workflow.publication_gate(merged, body, inline, 312, Path("/tmp"))
        with patch.object(workflow, "run_command", return_value='{"id":123,"html_url":"https://github.com/x"}') as runner:
            published = workflow.publish("owner/repo", 312, "81ac79e", body, inline)
            self.assertEqual(123, published['review_id'])
            payload = json.loads(runner.call_args.kwargs["input_text"])
            self.assertEqual("COMMENT", payload["event"])
            self.assertEqual("81ac79e", payload["commit_id"])
            self.assertEqual(inline, payload["comments"])
            self.assertIn("repos/owner/repo/pulls/312/reviews", runner.call_args.args[0])

    def test_review_snapshot_mismatch_blocks_before_discovery_comments(self):
        pr = self.fixture('pr/simple.json')
        with patch.object(workflow, 'pages') as pages:
            with self.assertRaisesRegex(RuntimeError, 'HEAD differs'):
                workflow.process_pr('owner/repo', pr, {'expected_head_sha': 'other'})
            with self.assertRaisesRegex(RuntimeError, 'base differs'):
                workflow.process_pr('owner/repo', pr, {'expected_base_sha': 'other'})
            pages.assert_not_called()

    def test_publication_requires_typed_id(self):
        for bad in (True, '123', None, 0):
            with self.subTest(bad=bad), patch.object(workflow, 'run_command', return_value=json.dumps({'id': bad, 'html_url': 'url'})):
                with self.assertRaisesRegex(RuntimeError, 'typed ID'):
                    workflow.publish('owner/repo', 1, 'a' * 40, 'body', [])

    def test_aggregation_supplies_changed_line_evidence(self):
        finding = self.fixture("reviews/code-review.json")["findings"][0]
        files = [{"filename": finding["file"], "patch": "@@ -10,2 +10,3 @@\n before\n+changed\n after\n", "lines": {11}}]
        finding = {**finding, "line": 11}
        evidence = workflow.changed_line_evidence(files, [finding])
        self.assertEqual([11], [item["line"] for item in evidence])
        self.assertIn("11: +changed", evidence[0]["patch_excerpt"])
        with patch.object(workflow, "codex_json", return_value={"findings": [finding], "summary": "Review"}) as run_codex:
            merged = workflow.aggregate([{"findings": [finding]}], {finding["file"]: {11}}, 312, Path("/tmp/pr"), files)
        self.assertEqual(1, len(merged["findings"]))
        self.assertIn("changed_line_evidence", run_codex.call_args.args[1])

    def test_supersede_checks_both_review_markers_before_patch(self):
        repo, number, sha = "owner/repo", 312, "a" * 40
        old = {"body": workflow.marker(repo, number, sha, version="v3"), "user": {"login": "reviewer"}}
        new = {"body": workflow.marker(repo, number, sha, version="v4"), "user": {"login": "reviewer"},
               "html_url": "https://github.com/owner/repo/pull/312#issuecomment-22"}
        with patch.object(workflow, "api", side_effect=[{"head": {"sha": sha}}, old, new]), \
             patch.object(workflow, "run_command", return_value=json.dumps({"html_url": "updated"})) as command:
            self.assertEqual("updated", workflow.supersede_v3_comment(repo, number, 11, 22))
        self.assertIn("--method", command.call_args.args[0])
        self.assertIn("PATCH", command.call_args.args[0])
        with patch.object(workflow, "api", side_effect=[{"head": {"sha": sha}}, old, {**new, "body": "wrong"}]), \
             patch.object(workflow, "run_command") as command:
            with self.assertRaisesRegex(RuntimeError, "Refusing to supersede"):
                workflow.supersede_v3_comment(repo, number, 11, 22)
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
