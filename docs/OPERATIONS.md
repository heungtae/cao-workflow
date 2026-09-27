# Operations

## Install and update

Run `make doctor`, `make validate`, `make test`, `make install`, then `make status`. `make update` repeats the same validate-before-deploy path. Deployment state is in `$CAO_HOME_DIR/cao-workflow-project-state.json`; it records only this project's owned resources and their installed hashes. Run scripts from any working directory.

`cao install` is used for profiles. Workflow files are atomically replaced because this CAO release has no workflow create/update command. `status` compares repository sources to CAO's local agent store, the CAO `agent-context` copy, and workflow files; it does not checksum provider-specific files outside CAO home. If a deployed file differs from the recorded hash, install and delete stop; reconcile manually after identifying its owner. Avoid editing runtime files directly.

## Rollback

Check out the prior Git revision in this repository and run `make update`. If the new revision changed the workflow's review semantics, restore or deliberately choose the workflow version before redeploying; marker identity includes that version. For a failed profile projection, inspect `cao profile show NAME` and rerun install after correcting the provider environment. Keep the previous Git commit available until deployment is verified.

## Uninstall

`./scripts/uninstall.sh [github-pr-review] --yes` prints its exact target list. It removes only resources present in this project's state with matching deployed hashes. It never removes built-in or unrelated profiles. Workflow deletion uses the verified local path because a remote CAO server might point at a different home; the index is derived from disk. Profile removal uses CAO's CLI and then removes the matching owned `agent-context` copy. Provider-specific files outside CAO home may require CAO-specific cleanup when changing providers.

## Observability and exit codes

`cao workflow run` prints a run id; `cao workflow status RUN_ID`, `events RUN_ID`, and `result RUN_ID` show execution. The workflow output includes repository, PR number, SHA, start/end timestamps, result, finding count, and publish destination. CAO's journal records step statuses. The management scripts use exit code 0 success, 1 runtime failure, 2 validation failure, 3 dependency/config/ownership failure. Logs never intentionally include tokens.

## CAO upgrade

Recheck `cao --version`, `cao workflow --help`, `cao profile --help`, profile validation, and workflow validation. Inspect any change to Codex provider sandbox behavior, workflow storage, script shim `step` contract, and input types. Validate and deploy in an isolated `CAO_HOME_DIR` before updating an active CAO home.

## Troubleshooting

`doctor` diagnoses missing commands, invalid GitHub authentication, the `$CODEX_HOME/cao_pr_review_readonly.config.toml` file, CAO config, and deployed resources. If `cao workflow validate` cannot reach the server, start `cao-server` and match `CAO_API_PORT`. If a large PR exceeds bounds, narrow the PR or revise reviewed limits with tests and a workflow version bump. A stale HEAD during review blocks publish; rerun after the new commit appears.
