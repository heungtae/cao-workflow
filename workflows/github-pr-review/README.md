# github-pr-review v6

CAO 2.5.0 script-tier workflow. The deployable file is [workflow.py](workflow.py). The manifest installs it as `$CAO_HOME_DIR/workflows/github-pr-review.py` and installs its five read-only Codex profiles first. The script invokes reviewers, aggregator, and publication gate through CAO `step()`. Each step receives only a shell-inert token; the review input is stored in an owner-only file. The deterministic publisher creates one GitHub COMMENT review containing inline comments and a final summary.

Run through the wrapper:

```bash
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
```

The wrapper accepts the options documented in [GitHub PR Review](../../docs/GITHUB-PR-REVIEW.md). [config.example.json](config.example.json) illustrates equivalent CAO input values; it is documentation, not a secret store or an automatically loaded runtime file. The workflow's static `INPUTS` declaration is the CAO runtime contract.

v6 returns a typed `review_id` and `review_url` for new publication. It pins both
HEAD and base when invoked by the [review → apply coordinator](../../scripts/run-review-apply.sh).
The base marker distinguishes a changed base at the same HEAD. A skipped review
must be resolved and authenticated separately before apply.
