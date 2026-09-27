import importlib.util
import json
import sys
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
        with patch.object(workflow, "pages", side_effect=[[{"body": identity}], []]), patch.object(workflow, "checkout") as checkout:
            result = workflow.process_pr("owner/repo", pr, {"publish_mode": "comment"})
        self.assertEqual("skipped", result["result"])
        checkout.assert_not_called()

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
        fake = types.SimpleNamespace(output=json.dumps({"findings": code["findings"] + code["findings"] + test["findings"], "summary": "Review"}))
        with patch.object(workflow, "step", return_value=fake):
            merged = workflow.aggregate(duplicates, {"src/consumer.py": {12}}, 312)
        self.assertEqual(2, len(merged["findings"]))
        body, request = workflow.render_review(merged, "81ac79e", "major", workflow.marker("owner/repo", 312, "81ac79e"))
        self.assertTrue(request)
        self.assertIn("Offset committed early", body)
        with patch.object(workflow, "step", return_value=types.SimpleNamespace(output='{"publish":true}')):
            workflow.publication_gate(merged, body, 312, Path("/tmp"))
        with patch.object(workflow, "step", return_value=types.SimpleNamespace(output='{"publish":false}')):
            with self.assertRaises(RuntimeError):
                workflow.publication_gate(merged, body, 312, Path("/tmp"))
        with patch.object(workflow, "run_command", return_value='{"html_url":"https://github.com/x"}') as runner:
            workflow.publish("owner/repo", 312, body, "review", request)
            self.assertIn("event=REQUEST_CHANGES", runner.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
