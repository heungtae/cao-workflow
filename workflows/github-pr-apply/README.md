# github-pr-apply v1

Apply an explicit, authenticated CAO v6 review at its original HEAD and base.
The default mode produces a candidate patch; `push` requires operator policy,
complete outcomes, passing isolated tests, and an unchanged unprotected branch.

The separate read-only `pr-review-applier` profile proposes one exact replacement
per supplied file. The deterministic workflow validates every replacement before
writing it. The model gets bounded source text and comments, never a checkout
path or a Git/GitHub write tool. Candidate source is removed after the run;
`candidate.patch` and `result.json` remain in a unique owner-only artifact directory.

Prefer the combined entry point:

```bash
./scripts/run-review-apply.sh --repository owner/repo --pr 312 \
  --policy /absolute/operator/apply-policy.json
```

For an explicit existing review:

```bash
./scripts/run.sh github-pr-apply --repository owner/repo --pr 312 \
  --head-sha ORIGINAL_40_CHARACTER_SHA --review-id 123456 \
  --policy /absolute/operator/apply-policy.json --apply-mode patch
```

Copy [apply policy example](../../config/apply-policy.example.json) to an operator
directory outside `/tmp/cao-pr-apply`, set mode `0600`, and fill the repository,
review author, editable paths, context files, prepared test image and argv commands.
Image references must use `repository@sha256:...` or an immutable local `sha256:...`
image ID. The image must already contain offline dependencies and tests must work
as the non-root CAO user. Docker must run on the same host as CAO's workspace.
The example's placeholder digest cannot be executed.

`context_paths` supplies existing related/test files; `new_files` explicitly
permits absent files to be created. Both must also match `editable_paths`.
The model cannot delete files or edit paths outside its supplied context. There
are caps of 40 context files, 40KB per text file, 120KB total model context, 100
inline findings, 300 changed files, and 2MB candidate patch. Cap hits fail closed.

Install [apply Codex config](../../config/cao_pr_apply_readonly.config.toml) into the
CAO server's `CODEX_HOME`. It enforces read-only sandbox and does not inherit the
shell environment. Trust the fixed carrier root; target PR checkouts are never
model working directories. The one-file tool restriction is profile policy,
not a filesystem read denylist: CAO's account must still be operated as a trusted
review service, as for the existing reviewers. No target code is run in that account.
Keep CAO memory injection empty/disabled for the shell-inert carrier protection,
as documented for the reviewers.

Tests use a disposable copy without `.git`/`.env`, no network or host credentials,
a read-only container root, no capabilities, and memory/CPU/process limits.
Test writes cannot alter the candidate subsequently committed. Tests produce
status and configured command names, not untrusted stdout in the workflow result.
Every original finding needs an outcome. `partial` produces exit 1 in the chain
and is never pushed; `applied` in patch mode means a tested candidate, not a push.

See the [implementation contract](../../docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md) and
[operations](../../docs/OPERATIONS.md) for recovery and runner configuration.
