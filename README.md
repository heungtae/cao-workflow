# CAO Workflow Management

Manage CAO workflows and agent profiles in Git, then deploy them to CAO home.
This repository is the source of truth; CAO home contains installed resources.

Start with [installation](#setup-and-installation), choose a
[workflow](#run-a-workflow), then use [status and results](#status-and-results)
or [troubleshooting](#troubleshooting). Commands below run from this repository's
root. Replace repository names, PR/Issue numbers, and policy paths with your values.

## Choose a workflow

All four workflows are registered in [manifest.json](manifest.json).
Their linked READMEs cover configuration, options, results, and recovery.

| Workflow | Use it to | Default GitHub effect | Additional setup |
| --- | --- | --- | --- |
| [github-pr-review v6](workflows/github-pr-review/README.md) | Review one PR or discover open PRs | Publish a `COMMENT` review with inline findings | Review Codex config |
| [github-pr-apply v1](workflows/github-pr-apply/README.md) | Apply a review, standalone or after review | Save a tested patch; push requires `--apply-mode push` | Apply Codex config, policy, Docker/test image |
| [mcp-exception-issue v1](workflows/mcp-exception-issue/README.md) | Analyze MCP exceptions against deployed source | Create or reuse evidence-supported Issues | Incident Codex config, policy, external MCP provider |
| [github-issue-fix v1](workflows/github-issue-fix/README.md) | Fix a manually selected open Issue | Push a new Issue branch and post a result comment | Incident Codex config, policy, Docker/test image; MCP for incident evidence |

PR review and apply can run together through a resumable coordinator. The two
incident workflows run independently; neither invokes the other. Models review
code and propose edits through read-only profiles. Deterministic workflow code
handles file changes and GitHub publication.

## Setup and installation

### 1. Prepare dependencies and authentication

- Linux/WSL and Python 3.11 or later.
- CAO 2.5.0 or later (`cao`, `cao-server`), Codex CLI and Codex authentication.
  The integration was inspected against CAO 2.5.0; revalidate CAO upgrades.
- `git`, `gh`, `jq`, and `tmux`.
- GitHub authentication through `gh auth login` or `GH_TOKEN`/`GITHUB_TOKEN`.
  Grant Contents/Pull requests/Issues read access as needed, Pull requests write
  for reviews, Issues write for Issues/comments, and Contents write for pushes.

The CAO server and launchers must use the same account, `CAO_HOME_DIR`, and
`CODEX_HOME`. Without an override, the manager resolves CAO home from
`cao config path`; Codex config files go in `~/.codex`.

### 2. Install the named read-only Codex configs

For all workflows, using the default `CODEX_HOME`:

```bash
mkdir -p ~/.codex
install -m 600 config/cao_pr_review_readonly.config.toml ~/.codex/
install -m 600 config/cao_pr_apply_readonly.config.toml ~/.codex/
install -m 600 config/cao_incident_readonly.config.toml ~/.codex/
```

For a custom `CODEX_HOME`, use that directory instead. These files are separate
from the agent profiles installed by the resource manager. They enforce Codex's
read-only sandbox; an agent's `allowedTools` list alone does not enforce it.
If you customize PR workspace roots, update their trusted paths in the configs.

Start the server in a separate terminal with memory injection disabled:

```bash
CAO_MEMORY_ENABLED=false cao-server
```

Incident workflows check the server's effective memory setting. PR workflows
also require disabled or empty memory injection for their fixed input carriers.

### 3. Validate, install, and inspect resources

```bash
make validate
make test
make install
make status
make doctor
```

`make install` installs all four workflows and their profiles, validates before
deployment, and skips identical resources. It does not configure authentication,
policies, Docker images, or an MCP server. Complete your workflow's additional
setup before running it.

To install only one workflow and its profiles, use
`./scripts/install.sh WORKFLOW_NAME`, choosing a name from the table above.
Installation and updates refuse to replace unmanaged or modified runtime files.

## Run a workflow

### Review a PR

```bash
# Run the model review without publishing.
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 --dry-run
```

Remove `--dry-run` to publish an inline `COMMENT` review. Omit `--pr` to discover
open, non-draft PRs. The same HEAD/base and workflow version is skipped when its
review marker exists; `--force-review` reruns it.
See [PR review](workflows/github-pr-review/README.md) for filtering and options.

### Review and apply findings

Prepare an [apply policy](config/apply-policy.example.json) and a preloaded test
image first. The coordinator publishes the review before applying it, including
in patch mode.

```bash
# Review → apply → test → retain a candidate patch (default apply mode).
./scripts/run.sh github-pr-review --repository owner/repository --pr 312 \
  --apply --policy /absolute/operator/apply-policy.json
```

Add `--apply-mode push` to push to the PR branch when policy and validation allow
it. See [PR apply](workflows/github-pr-apply/README.md) for policy setup, standalone
apply, chain recovery, and the manual GitHub Action.

### Analyze MCP exceptions and create Issues

Prepare the external provider and [incident policy](config/incident-policy.example.json).

```bash
# Analyze without publishing or changing the operational checkpoint.
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json --dry-run
```

Remove `--dry-run` to publish supported Issues and retain monitor state.
See [MCP exception → Issue](workflows/mcp-exception-issue/README.md) for provider
bindings, historical intervals, cron, and evidence limits.

### Fix an open Issue

Prepare the incident policy's fix settings and a preloaded test image.

```bash
# Develop and test a patch without GitHub writes.
./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json --apply-mode patch
```

Remove `--apply-mode patch` to use the default **push** mode: create an Issue
branch and post a result comment. See [Issue fix](workflows/github-issue-fix/README.md)
for admission, regression tests, artifacts, and recovery. It does not create a PR
or close the Issue.

## Status and results

Resource installation and workflow execution have separate status commands:

| What to inspect | Command |
| --- | --- |
| This repository's resource inventory | `make list` |
| This project's deployments in the selected CAO home | `make status` |
| Dependency/auth/config/registry diagnostics | `make doctor` |
| CAO's registered specs / available profiles | `cao workflow list` / `cao profile list` |
| Recent executions and run IDs | `cao workflow runs --limit 20` |
| One execution's state | `cao workflow status RUN_ID` |
| Recorded events / live progress | `cao workflow events RUN_ID --no-follow` / `cao workflow events RUN_ID` |
| Retained output and errors | `cao workflow result RUN_ID --json` |

`make status` reports each workflow/profile as follows:

| Status | Meaning / next action |
| --- | --- |
| `up-to-date` | Owned deployment matches the Git source |
| `missing` | Resource is absent; install or update |
| `outdated` | Owned deployment differs from the current Git source; update |
| `unmanaged` | File exists without this project's ownership record; identify its owner |
| `modified` | Deployed file differs from its recorded hash; inspect runtime changes |
| `context-missing-or-modified` | Agent context copy is absent or changed; inspect it before redeployment |

`make doctor` checks common dependencies, GitHub auth, PR Codex configs,
deployment ownership/hashes, and the CAO registry. It does not qualify MCP
bindings, incident config, operator policies, or test images.

A CAO state of `completed` means the script returned. Inspect `output` as well:
PR workflows use `result` (review has a `results` array); incident workflows use
`status`, including per-incident statuses. Partial, blocked, deferred, or
pending-publication work can remain in a completed run.

For interrupted combined runs or incident submissions, keep the printed state
or journal path and follow the workflow README's resume instructions.
A lost connection does not prove the server-side run stopped.

## Update and uninstall

After updating this Git checkout, run `make update`, then `make status`.
For selected resources, use `./scripts/update.sh WORKFLOW_NAME` or
`./scripts/uninstall.sh WORKFLOW_NAME --yes`. To remove all project resources:

```bash
./scripts/uninstall.sh --yes
```

Ownership and SHA-256 hashes are recorded in `cao-workflow-project-state.json`
under CAO home. Uninstall refuses modified resources by default. After inspecting
the changes, `--yes --force` bypasses modification checks for owned resources;
ownership checks still apply. See [operations](docs/OPERATIONS.md) for rollback
and provider-specific cleanup.

## Troubleshooting

Start with `make status`, `make doctor`, and `cao workflow result RUN_ID --json`.

| Symptom | Check / action |
| --- | --- |
| Missing command or authentication error | Install the reported dependency; verify `gh auth status` and Codex authentication under the CAO account |
| Server/registry connection error | Start `cao-server`; match `CAO_API_PORT` and CAO home between server and launcher |
| Named Codex profile missing or invalid | Install the workflow's config in the server/launcher's `CODEX_HOME`; preserve read-only settings |
| Folder trust prompt / startup timeout | For PR workflows, match the trusted fixed workspace root in the config; see [operations](docs/OPERATIONS.md#troubleshooting) |
| `missing` / `outdated` deployment | Install/update the selected workflow, then inspect `make status` |
| `unmanaged` / `modified` / context mismatch | Compare runtime files, ownership state, and Git sources before reconciling; normal update will not overwrite them |
| Policy, Docker, or MCP admission failure | Use the workflow README's setup checklist; example policies contain placeholders |
| CAO completed, but no expected review/Issue/push | Inspect workflow output, per-item statuses, and artifacts; completed alone does not establish publication |
| Connection lost or publication outcome uncertain | Preserve state and resume the recorded execution using the workflow-specific guide |

## Further documentation

- [Operations](docs/OPERATIONS.md): deployment, rollback, CAO upgrades, apply runner setup.
- [Incident operations](docs/INCIDENT-WORKFLOWS.md): provider qualification, policy contracts, recovery.
- [External MCP log server specification](docs/MCP-LOG-SERVER-SPEC.md): provider contract; server implementation and deployment are external.
- [Architecture](docs/ARCHITECTURE.md) and [workflow development](docs/WORKFLOW-DEVELOPMENT.md).
- Design references: [review → apply](docs/GITHUB-PR-REVIEW-APPLY-DESIGN.md),
  [exception → Issue](docs/MCP-EXCEPTION-ISSUE-DESIGN.md), [Issue fix](docs/GITHUB-ISSUE-FIX-DESIGN.md).

`agents/` contains profiles, `workflows/` contains workflow sources, `config/`
contains examples, `scripts/` contains launchers/resource management, and `tests/`
contains validation fixtures. Keep credentials, personal CAO state, target
checkouts, and `.env` files outside this repository. Example JSON inputs are
reference material; launchers do not automatically load them.
