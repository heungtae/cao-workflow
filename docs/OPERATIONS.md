# Operations

## Install and update

Run `make doctor`, `make validate`, `make test`, `make install`, then `make status`. `make update` repeats the same validate-before-deploy path. Deployment state is in `$CAO_HOME_DIR/cao-workflow-project-state.json`; it records only this project's owned resources and their installed hashes. Run scripts from any working directory.

`cao install` is used for profiles. Workflow files are atomically replaced because this CAO release has no workflow create/update command. `status` compares repository sources to CAO's local agent store, the CAO `agent-context` copy, and workflow files; it does not checksum provider-specific files outside CAO home. If a deployed file differs from the recorded hash, install and default uninstall stop; reconcile manually after identifying its owner, or use the explicit uninstall override described below. Avoid editing runtime files directly.

## Rollback

Check out the prior Git revision in this repository and run `make update`. If the new revision changed the workflow's review semantics, restore or deliberately choose the workflow version before redeploying; marker identity includes that version. For a failed profile projection, inspect `cao profile show NAME` and rerun install after correcting the provider environment. Keep the previous Git commit available until deployment is verified.

## Uninstall

`./scripts/uninstall.sh [github-pr-review] --yes` prints its exact target list. It removes only resources present in this project's state with matching deployed hashes. It never removes built-in or unrelated profiles. Workflow deletion uses the verified local path because a remote CAO server might point at a different home; the index is derived from disk. Profile removal uses CAO's CLI and then removes the matching owned `agent-context` copy. Provider-specific files outside CAO home may require CAO-specific cleanup when changing providers.

Add `--force` to remove resources recorded as owned by this project even when the resource or profile context has been modified. The command prints a warning for each modified resource. This override skips the modification checks; ownership checks and the confirmation requirement remain. Use `--yes --force` for non-interactive removal.

## Observability and exit codes

`cao workflow run` prints a run id; `cao workflow status RUN_ID`, `events RUN_ID`, and `result RUN_ID` show execution. CAO records each reviewer, aggregator, and publication gate `step()` in the run journal. The workflow output includes repository, PR number, SHA, start/end timestamps, result, finding count, and publish destination. The management scripts use exit code 0 success, 1 runtime failure, 2 validation failure, 3 dependency/config/ownership failure. Logs never intentionally include tokens.

## CAO upgrade

Recheck `cao --version`, `cao workflow --help`, `cao profile --help`, profile validation, and workflow validation. Inspect any change to the CAO `step()` shim, Codex provider startup/status behavior, workflow storage, and input types. Validate and deploy in an isolated `CAO_HOME_DIR` before updating an active CAO home.

## Troubleshooting

`doctor` diagnoses missing commands, invalid GitHub authentication, the `$CODEX_HOME/cao_pr_review_readonly.config.toml` file, CAO config, and deployed resources. If `cao workflow validate` cannot reach the server, start `cao-server` and match `CAO_API_PORT`. If Codex reports a folder trust problem, check that the fixed `workspace_root` has mode `0700` and its exact path is trusted in the named Codex profile; never trust individual PR checkout directories. CAO may mistake a Codex bootstrap failure for an idle shell; the workflow's step carrier is a fixed shell no-op token, and missing JSON fails before publication. CAO can prepend curated memory ahead of that carrier, so keep memory injection disabled or verify that this workflow receives an empty memory block. If a large PR exceeds bounds, narrow the PR or revise reviewed limits with tests and a workflow version bump. A stale HEAD during review blocks publish; rerun after the new commit appears.

The incident-specific `supersede_v3_comment` publisher helper corrects an empty v3 comment only after it verifies the old and replacement comments have matching repository, PR, HEAD, workflow markers, and author. It does not run during normal reviews.

## Review → apply setup and recovery

Install/update both manifest workflows and six profiles. Configure both named
read-only Codex configs in the CAO server's `CODEX_HOME`; the apply config also
uses `shell_environment_policy.inherit=none`. The coordinator rejects missing,
modified, or outdated deployments before starting a review. Standalone apply is
available through `scripts/run.sh github-pr-apply` with explicit PR, HEAD, review
ID, and policy; it authenticates the selected review again.

Copy `config/apply-policy.example.json` into an operator-controlled directory
outside the model workspace and set mode `0600`. Set the actual review bot login,
editable file patterns, optional related context/new files, and at least one test
command expressed as an argv array. Test dependencies must already be available
in a local digest-pinned image (or immutable image ID); there is no image pull or
network dependency installation during apply. Docker must run on the CAO host;
run CAO as a non-root user with the intended Docker access. The coordinator checks
that the configured image exists before spending a review run.

For `push`, fill explicit `push_branches` and Git author fields. An empty allowlist
denies every push. Forks, PR base/default branches, protected branches, stale
HEAD/base, partial outcomes, and failed tests cannot be pushed. The new commit must
have exactly the reviewed HEAD as its single parent; an explicit expected-SHA
`--force-with-lease` atomically rejects branch deletion, rewinds and concurrent
updates. This parent check forbids history rewriting. Hooks are disabled and
global/system Git configuration is excluded. A successful push returns its verified
commit SHA and includes review ID, original HEAD, apply key and CAO run trailers.
Retries authenticate those trailers against the retained completed CAO result;
missing/expired journal data requires manual reconciliation. No approval or merge
is performed.

The coordinator prints its mode-0600 state path before submission. A lost CLI
connection does not mean a child run died. Use `--resume STATE_PATH` (with the same
`--state-root` if customized) to query the retained explicit run IDs. Policy and
workflow version changes reject an old state. Failed/cancelled terminal runs are
not silently resubmitted: use a new chain after correcting the cause. This resolves
the unique prior review and starts a fresh candidate. Native CAO step resume is
manual and is not the coordinator's retry mechanism.

SIGINT/SIGTERM cancellation is forwarded to the active CAO run and its resulting
state is recorded. Test containers carry the apply run ID; cancellation also
removes containers with that exact workflow/run label if the worker was killed
before cleanup. Same-host PR locks serialize chains and applications; direct
standalone reviews do not acquire the chain lock, so use one operational trigger
per PR. Failed candidate `result.json` records its failing stage and artifact path.
CAO `completed` for apply means its script returned; inspect its `result` field.
`partial` is not full success and the coordinator exits 1 for it.

The manual Action requires a trusted pre-provisioned checkout at
`CAO_WORKFLOW_PROJECT_ROOT` and an absolute policy at `CAO_APPLY_POLICY_PATH`
(repository Actions variables). The local checkout HEAD must equal the dispatched
manager commit SHA, and its runtime resources must be current. The Action never
checks out the target PR as workflow code. Use a trusted non-root self-hosted runner
labelled `linux` and `cao`, and run the coordinator and CAO with the same filesystem
and policy access. Candidate artifacts and chain state stay on that host; after
inspection, retain or remove only the intended owner-controlled artifact folders.

The optional real isolation gate is:

```bash
CAO_TEST_IMAGE=sha256:YOUR_LOCAL_IMAGE_ID python3 -m unittest discover -s tests -p test_apply_isolation.py -v
```

It requires a preinstalled Python 3 image and verifies no external network,
credential environment, `.git`/`.env` mount, or mutation of the original candidate.
Local unit/installation/isolation evidence does not establish real provider output
quality or live GitHub publication; qualify those separately against a test PR.

### Combined Execution Through run.sh

```bash
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --apply \
  --policy /absolute/operator/apply-policy.json
./scripts/run.sh github-pr-review --repository owner/repo --pr 312 --force-review --apply \
  --policy /absolute/operator/apply-policy.json --apply-mode push
```

`--publish-mode review` can be used with combined execution. Dry-run/no-publish,
PR discovery, and `--detach` are unsupported. For standalone apply, use
`run.sh github-pr-apply --pr ... --review-id ... --head-sha ... --policy ...`
with the required repository argument. Combined execution replaces the launcher
process with the coordinator, preserving exit codes and cancellation signals.

## Incident workflows

See [Incident workflow operations](INCIDENT-WORKFLOWS.md) for external MCP provider
bindings, independent workflow installation, private policies, manual/cron
execution, and durable publication recovery.
## GitHub polling automation

See [polling setup, services and recovery](GITHUB-POLLING-OPERATIONS.md) for the
repository-managed collector/Worker CLI and systemd user templates.
