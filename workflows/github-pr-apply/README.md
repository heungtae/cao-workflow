# github-pr-apply v2

Apply an authenticated CAO v7 review at its original PR HEAD and base. The default
`patch` mode retains a tested candidate. Explicit `push` mode updates the PR
branch only after policy checks, complete outcomes, and passing isolated tests.

[All workflows and common setup](../../README.md) ·
[Detailed operations](../../docs/OPERATIONS.md#review--apply-setup-and-recovery)

Commands below run from the repository root. Replace repository/PR values and paths.

## Prepare and install

1. Complete [common setup](../../README.md#setup-and-installation). Install both
   review and apply Codex configs in the server/launcher's `CODEX_HOME`, with
   read-only sandbox and memory injection disabled or empty. Apply config also
   sets `shell_environment_policy.inherit=none`.
   Authenticate with `gh auth login` as the CAO account; launcher tokens are
   not forwarded to CAO 2.5.0 workflow scripts.
2. Copy [apply-policy.example.json](../../config/apply-policy.example.json) to an
   absolute operator-controlled path outside model workspaces. Set mode `0600`
   and replace all placeholders. The launcher and CAO process must both read it.
3. Configure the repository's policy fields below.
4. Prepare Docker on the CAO host and preload the configured image. Tests must
   run as the non-root CAO user with all dependencies already in the image.

| Policy field | What to configure |
| --- | --- |
| `review_authors` | GitHub logins permitted to author the selected CAO review |
| `editable_paths` | Paths the candidate may change |
| `context_paths` / `new_files` | Existing related/test files and explicitly permitted new files; both must match editable paths |
| `test_image` | Preloaded `repository@sha256:...` or local immutable `sha256:...` image ID |
| `test_commands` | At least one test command as an argv array; dependencies must work offline |
| `push_branches` | Explicit PR branch allowlist for push; the example's empty list denies every push |
| `git_author_name` / `git_author_email` | Commit author for push mode |

The example image digest is a placeholder. Tests use a disposable copy with no
network, host credentials, `.git`/`.env`, or mount of the original candidate;
the container has a read-only root and resource limits.

```bash
./scripts/install.sh github-pr-review
./scripts/install.sh github-pr-apply
make status
```

Apply installs `workflow.py` and the `pr-review-applier` profile. The model
proposes exact replacements from bounded text; workflow code validates/writes them.

## Review and apply together

```bash
# Publish a review, apply it, test, and retain a candidate patch.
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json

# Also push when every policy and validation condition passes.
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json --apply-mode push
```

**Patch mode still publishes the review in combined execution.** It only suppresses
candidate push. Use PR review's standalone dry-run when you want no GitHub writes.

The coordinator pins HEAD/base and authenticates the selected review, including
when review was skipped because its marker already exists. Add `--force-review`
for a fresh review. `--model` and `--publish-mode review` are accepted. Discovery,
`--dry-run`, `--no-publish`, `--detach`, and standalone apply inputs are unsupported.
Use one explicit `--pr`.

The underlying coordinator also accepts `--state-root`, `--review-workspace`, and
`--apply-workspace` through `./scripts/run-review-apply.sh`. Custom workspace roots
must match trusted paths in the Codex configs. Defaults are
`/tmp/cao-pr-review-apply`, `/tmp/cao-pr-review`, and `/tmp/cao-pr-apply` respectively.

## Apply an existing review

```bash
./scripts/run.sh github-pr-apply --repository owner/repository --pr 312 \
  --head-sha ORIGINAL_40_CHARACTER_LOWERCASE_HEX_SHA --review-id 123456 \
  --policy /absolute/operator/apply-policy.json --apply-mode patch
```

Use the original full HEAD SHA and GitHub review ID from a published v7 review.
The workflow authenticates its author, markers, base snapshot, and inline findings
again. Optional standalone flags are `--apply-mode push`, `--workspace-root PATH`,
`--model MODEL`, `--expected-findings N`, and `--detach`. Review-only flags do not
apply. [config.example.json](config.example.json) shows direct CAO inputs and is
not automatically loaded.

## Check results and artifacts

The coordinator prints a `state_path` before submitting review/apply runs.
Read its recorded run IDs and inspect them with:

```bash
cao workflow status RUN_ID
cao workflow result RUN_ID --json
```

CAO `completed` alone does not establish a successful application. Inspect the
apply workflow's `result` and `apply_mode`. CAO 2.5.0's retained result
omits run-level `output`; combined runs and polling retain it in
`child_outputs[RUN_ID]` in the printed chain journal. Compare that output with
the matching CAO terminal state. Standalone apply has no chain fallback; inspect
live script output and retained candidate `result.json`, and verify any remote
commit before retrying. Missing evidence does not establish a successful push.

| Result | Meaning |
| --- | --- |
| `applied` + `patch` | Validated, tested candidate; no push |
| `applied` + `push` | Pushed commit; inspect verified `commit_sha` |
| `partial` | Some findings remain unresolved; never pushed; coordinator exits 1 |
| `skipped` | No findings or an authenticated previous application |
| `failed` | Application/verification failed; inspect stage, checks, and retained artifacts |

Candidates are stored in unique `/tmp/cao-pr-apply/candidate-*` directories unless
the workspace is customized. `artifact_directory`, `patch_path`, and retained
`result.json` identify the output. Temporary source checkouts are removed. A
failure before candidate creation may have no artifact directory.

## Resume or retry

For an interrupted combined run, use the exact printed state path:

```bash
./scripts/run-review-apply.sh --resume /tmp/cao-pr-review-apply/CHAIN.json
```

If the chain used a custom state root, supply the same `--state-root`. State files
are owner-only (`0600`). Resume queries the recorded run IDs; policy/version
changes block new execution steps; already submitted apply outcomes are
reconciled using frozen evidence first. Resolve any publication intent before
starting another chain after a failed or cancelled run. Standalone apply has no chain state; inspect
its retained CAO result and artifacts before retrying, especially after a push.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing/modified/outdated deployment | Inspect `make status`; update owned resources after resolving modifications |
| Review cannot be authenticated | Verify review ID, author allowlist, v7 marker, original HEAD and base; create a fresh review for a new snapshot |
| Docker/image/test failure | Check same-host Docker access, immutable preloaded image, offline dependencies, and non-root test permissions |
| Path/context rejection | Match editable paths and explicitly configure related context/new files |
| Push denied | Check branch allowlist; forks, protected/base/default branches, partial outcomes, failed tests, and stale HEAD/base block push |
| Interrupted or uncertain push | Inspect recorded run/result and remote commit before retry; retain chain state/artifacts for reconciliation |

Limits include 40 context files, 40 KB per file, 120 KB total model context,
100 inline findings, 300 changed files, and a 2 MB patch. Exceeding them blocks apply.
See [operations](../../docs/OPERATIONS.md) for cancellation and recovery details.

## Automatic polling

Follow [polling setup and recovery](../../docs/GITHUB-POLLING-OPERATIONS.md) for
the separate collector, SQLite queue and single Worker. Automatic jobs use
policy-authorized push mode and force a fresh review for each admitted identity.
Disable competing automatic review/apply triggers for the same PRs. Before
updating dependencies, stop the user services and resolve retained executions;
stopping the Worker does not cancel submitted CAO runs.

## Manual GitHub Action

[pr-review-apply.yml](../../.github/workflows/pr-review-apply.yml) runs the same
launcher on a trusted non-root runner labelled `self-hosted`, `linux`, and `cao`.
Set repository variables `CAO_WORKFLOW_PROJECT_ROOT` and `CAO_APPLY_POLICY_PATH`.
The manager checkout must match the dispatched commit SHA, with current runtime
resources and same-host CAO/policy access. Artifacts and state stay on that host.
See [runner setup](../../docs/OPERATIONS.md#review--apply-setup-and-recovery).

The [implementation design](../../docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md) describes
review authentication, replacement validation, and publication contracts.
