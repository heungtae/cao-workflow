# github-issue-fix v1

Develop and test a fix for a manually selected open Issue. The default **push**
mode creates a new `cao/issue-N-KEY` branch and posts a result comment. Select
`--apply-mode patch` to retain a tested patch without GitHub writes.

The workflow runs independently of the exception monitor. It does not create a
PR, close the Issue, approve, or merge. Models propose exact replacements through
a read-only profile; deterministic code validates edits, tests, and publication.

[All workflows and common setup](../../README.md) ·
[Incident operations](../../docs/INCIDENT-WORKFLOWS.md)

Commands below run from the repository root. Replace repository, Issue, and paths.

## Prepare and install

1. Complete [common setup](../../README.md#setup-and-installation). Install
   [cao_incident_readonly.config.toml](../../config/cao_incident_readonly.config.toml)
   in the server/launcher's `CODEX_HOME` with mode `0600`. Start the server with
   `CAO_MEMORY_ENABLED=false`; the workflow checks its effective memory setting.
2. Copy [incident-policy.example.json](../../config/incident-policy.example.json)
   outside Git, replace placeholders, and set mode `0600`. Policy and token files
   must be owned by the CAO account. Use absolute private state/workspace paths
   (mode `0700`) without symlink components. Keep state, policy, and credentials
   outside model workspaces.
3. Configure fix policy and Issue admission as described below.
4. Prepare same-host Docker access for the non-root CAO account and a preloaded,
   digest-pinned test image with offline dependencies. Every fix must edit a
   designated regression-test path and provide regression scenarios.
5. Grant Contents and Issues read access, plus Contents/Issues write access for
   branch push and the result comment.

| Policy field | What to configure |
| --- | --- |
| `state_root` / `workspace_root` | Persistent private state and separate candidate workspaces |
| `base_branch` | Branch whose pinned source is used; defaults to the repository default branch |
| `source_paths` / `editable_paths` | Selected source/test context and permitted edit patterns |
| `regression_test_paths` / `new_files` | Required regression-test edit paths and explicitly permitted new files |
| `test_image` / `test_commands` | Preloaded immutable image and mandatory test argv arrays |
| `git_author_name` / `git_author_email` | Commit author |
| `allow_code_issues` | Permit ordinary code Issues without incident logs |
| `incident_publishers` / `issue_incidents` | Verified incident producer or explicit operator mapping for incident Issues |

For ordinary code Issues, enable `allow_code_issues` as intended. Incident Issues
need verified producer/replay metadata or an operator mapping. Their evidence is
re-queried through MCP against authoritative deployment metadata; pasted Issue
logs do not serve as evidence. Configure the provider and CAO-environment MCP SDK,
`httpx`, and `jsonschema` as described in
[incident setup](../../docs/INCIDENT-WORKFLOWS.md#setup).

```bash
./scripts/install.sh github-issue-fix
make status
```

Installation deploys `workflow.py` and the `issue-fixer` profile. The monitor need
not be installed to run this workflow.

## Run

```bash
# Develop and test a candidate without GitHub writes.
./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json --apply-mode patch

# Develop, test, push a new branch, and publish a result comment (default).
./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json
```

Use one positive `--issue` number for an open Issue. `--dry-run`, `--monitor`,
and historical interval flags belong to the monitor; use `--apply-mode patch`
for this workflow's no-publication mode. Patch and push have separate execution
identities: a patch run is not promoted directly; push runs develop/validate
under their own identity.

Tests use a disposable copy with no network, host credentials, `.git`, or mount
of the original candidate. Push rechecks the Issue specification and base SHA,
creates exactly one commit on the validated base, and verifies the remote SHA.
A competing branch cannot be overwritten.

## Check results and artifacts

The launcher prints `Execution journal: ...` before submission. Its journal
contains `run_ids`; inspect the run with:

```bash
cao workflow status RUN_ID
cao workflow result RUN_ID --json
```

Inspect `output.status`, not only the CAO run state or launcher exit code:

| Status | Meaning / next action |
| --- | --- |
| `patch_ready` | Tested candidate retained; no push in patch mode |
| `pushed` | Branch push verified and result comment published; inspect `branch`, `commit_sha`, and `comment_id` |
| `pushed_comment_pending` | Branch was pushed; result comment still needs reconciliation |
| `reconciling` | Remote publication is uncertain; preserve state and resume |
| `needs_context` / `blocked` / `failed` | More evidence, operator correction, or failure inspection is required |

`artifact_path` identifies the private `issue-fix-*` candidate directory under
policy `workspace_root`. The generated `candidate.patch` is retained there after
successful editing/tests. Execution records under `state_root/issue-executions/`
retain tests, snapshot identity, and publication state. Keep these with the
submission journal while investigating failures or pending publication.

## Resume or retry

For interrupted submissions or pending publication, use the printed journal:

```bash
./scripts/run.sh github-issue-fix --resume /absolute/operator/state/executions/EXECUTION.json
```

Resume rejects input overrides and checks the recorded run. Completed runs with
pending publication can create a recorded recovery-only run, which reconciles
without rerunning development, tests, or Git pushes. Failed/cancelled CAO runs
require explicit manual recovery. Policy/resource changes can restrict recovery
to reads while preserving observed remote outcomes.

After correcting a terminal attempt or supplying new evidence, explicitly retry:

```bash
./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json --apply-mode patch \
  --retry-reason "Preloaded the required test dependencies"
```

Select the intended apply mode again. The retry retains the prior execution and
uses a new attempt identity where permitted. Prior ambiguous publication is
reconciled first; retry flags do not authorize overwriting a competing branch.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Issue admission rejected | Verify open state, repository, `allow_code_issues`, or verified incident producer/operator mapping |
| Missing context / incident evidence | Configure selected source/test files and authoritative MCP revision/evidence; copied Issue logs are insufficient |
| Edit/regression gate rejected | Match editable paths and allowed new files; the fix must edit a designated regression-test path |
| Docker/test failure | Check same-host Docker access, preloaded digest-pinned image, offline dependencies, and non-root permissions |
| Issue/base changed before push | Inspect the new specification/base and start a run for that snapshot |
| Generated branch already exists | Reconcile retained ownership; the workflow will not overwrite a competing branch |
| Push succeeded but comment missing | Resume the journal and inspect `pushed_comment_pending`; do not repeat development/push to recover a comment |

For policy ownership, memory admission, deployment, or server problems, see
[common troubleshooting](../../README.md#troubleshooting) and
[incident recovery](../../docs/INCIDENT-WORKFLOWS.md#recovery-and-evidence-limits).
[config.example.json](config.example.json) documents direct CAO inputs; it is not
automatically loaded. The [design reference](../../docs/GITHUB-ISSUE-FIX-DESIGN.md)
describes admission, regression validation, and publication contracts.
