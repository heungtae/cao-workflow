# github-pr-review v1

CAO 2.5.0 script-tier workflow. The deployable file is [workflow.py](workflow.py). The repository manifest installs it as `$CAO_HOME_DIR/workflows/github-pr-review.py` and installs its five Codex profiles first.

Run through the wrapper:

```bash
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --dry-run
```

The wrapper accepts the options documented in [GitHub PR Review](../../docs/GITHUB-PR-REVIEW.md). [config.example.json](config.example.json) illustrates equivalent CAO input values; it is documentation, not a secret store or an automatically loaded runtime file. The workflow's static `INPUTS` declaration is the CAO runtime contract.
