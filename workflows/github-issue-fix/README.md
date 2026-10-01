# GitHub Issue fix workflow

The independent `github-issue-fix` script accepts a manually selected open Issue,
retrieves source from GitHub, proposes exact replacements through a read-only
profile, validates the candidate in isolated containers, and pushes a new
`cao/issue-N-KEY` branch with a result comment. It does not create a PR, close
the Issue, approve, merge, or invoke the exception monitor.

See [incident operations](../../docs/INCIDENT-WORKFLOWS.md).

```bash
./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json --apply-mode patch
```
