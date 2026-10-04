# Local GitHub polling operations

The initial integration collects open PRs and runs `github-pr-review` v7 followed
by `github-pr-apply` v2 in policy-authorized push mode. Collection and execution
are separate processes. Source and Handler implementations are registered in
repository code; configuration cannot load a module or execute a command.

## Setup

Use one non-root service account for CAO, collection and the Worker. Authenticate
that account with `gh auth login`. The CAO workflow process does not inherit
launcher `GH_TOKEN`; its own `gh` credential store must work. Install both named
read-only Codex configurations and the owned PR workflows using the README's
installation/update instructions. Existing deployment ownership checks remain
authoritative; these commands never replace unmanaged or modified resources.

Copy [automation.example.json](../config/automation.example.json) and
[apply-policy.example.json](../config/apply-policy.example.json) outside the
checkout, replace the example paths/repository, and configure review authors,
editable paths, allowed push branches, Git author, a digest-pinned Docker image,
and isolated validation commands. Policy/configuration files must be owned by
the service account with mode `0600`; the absolute state directory must have mode
`0700`. The CLI creates new state directories and files with those modes and
rejects existing broader permissions or symlinks. Do not store credentials in
the JSON files.

```bash
python3 scripts/automation.py --config /absolute/automation.json doctor
python3 scripts/automation.py --config /absolute/automation.json poll --dry-run
python3 scripts/automation.py --config /absolute/automation.json poll
python3 scripts/automation.py --config /absolute/automation.json status --json
python3 scripts/automation.py --config /absolute/automation.json worker --once
```

`doctor` checks authentication, CAO connectivity, owned/current PR deployments,
read-only Codex configurations, the configured policy, and the test image.
`poll --dry-run` copies existing queue/cache state into memory, performs collection,
and prints proposed jobs without changing durable files or launching CAO.
The first real poll includes existing eligible open PRs. Drafts, forks and
non-allowlisted branches are excluded; publication also checks protected/default
branches. Collection defaults to three minutes and follows every page, including
remaining pages after a `304`. An incomplete collection retains the previous
checkpoint and pending jobs; status reports the error and the next retry time.

Each enabled repository must have one automatic execution path. Disable any
existing automatic Action/cron/monitor trigger that executes review/apply for
the same PRs before enabling this timer. The repository's Action remains manual.
Configuration rejects duplicate PR Sources and duplicate Source/event Bindings.

## User services

Copy the three templates in [systemd](../systemd) into
`~/.config/systemd/user/`. Create an owner-only
`~/.config/cao-automation/environment` containing the following substitutions:

```ini
CAO_AUTOMATION_CHECKOUT=/absolute/cao-workflow
CAO_AUTOMATION_CONFIG=/absolute/automation.json
PATH=/home/operator/.local/bin:/usr/local/bin:/usr/bin:/bin
```

Set `CAO_HOME_DIR`, `CAO_API_PORT` and `CODEX_HOME` there only when they differ
from the CAO server account's defaults. No token is needed in this file.
Then enable the services:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cao-automation-poll.timer cao-automation-worker.service
systemctl --user status cao-automation-poll.timer cao-automation-worker.service
journalctl --user -u cao-automation-poll.service -u cao-automation-worker.service
```

The Worker owns a nonblocking process lock and processes one active execution
globally. Collection continues while a workflow is running. Reconciliation takes
priority over new jobs. Stopping the Worker leaves already submitted CAO runs
owned by CAO; a restarted Worker observes the same journal and run IDs.

```bash
systemctl --user stop cao-automation-poll.timer cao-automation-worker.service
systemctl --user disable cao-automation-poll.timer cao-automation-worker.service
```

Disabling services does not uninstall owned CAO resources or delete retained
execution evidence. Use the existing ownership-checked uninstall command for
resource removal when no execution needs reconciliation.

## Recovery and changes

`automation.sqlite3` owns observations, cached pages, jobs and attempt history.
`policies/` retains exact policy bytes. `chains/<32-hex-id>.json` owns CAO run IDs,
the request, expected dependency hashes, and any push intent. Candidate artifacts
remain under the automation apply workspace. Preserve all of these until an
execution is resolved. Do not delete or edit them to force another run.

Jobs are `queued`, `running`, or `reconciling`, followed by `completed`, `skipped`,
`superseded`, `failed`, or `blocked`. Status includes collection health/times,
queue state, attempts, journal locations and retained CAO IDs. Collection health
does not establish model/provider or publication success. Registered future
Handlers can report their own health separately; tick observation is not treated
as evidence of MCP ingestion.

A new HEAD/base, Binding, policy, or relevant workflow/profile/Codex configuration
creates another job identity. Titles/comments and unrelated manifest entries do
not. Admission rechecks the frozen identity. Child workflows check policy,
Binding and dependencies at admission and before publication. A change blocks
new writes. Retained CAO outcomes can still be reconciled without using new
workflow or policy bytes.

Successful output commits are recorded as handled only with retained execution
evidence for the same base/configuration. Pending jobs for that exact output are
superseded. Bot names and commit trailers alone cannot suppress a job. A user
commit or base/configuration change is eligible again.

Transport ambiguity remains `reconciling` and uses the same IDs. Missing journals,
identity mismatches, and unresolved push intent become `blocked`. If a process
dies after push, retained publication intent plus the exact remote commit and
CAO terminal result can establish the outcome. If the remote no longer identifies
that commit, manual reconciliation is required. No terminal failure or partial
application is automatically retried.

```bash
python3 scripts/automation.py --config /absolute/automation.json retry JOB_ID
```

Retry first reconciles the previous attempt and possible publication. It queues
a new attempt only when the previous execution is terminal, publication is known
safe, and the original job still passes admission. A changed policy/resource
supersedes the old job; poll to collect the new identity. All previous attempt
IDs and outcomes remain recorded. For an unchanged active CAO run, recover the
CAO service and restart the Worker instead of retrying the job.

CAO 2.5.0 does not return a retained run-level output from its result endpoint.
The PR workflows save that output into the private chain journal before emitting
it. The coordinator requires both this evidence and the matching CAO terminal
state. A missing previously acknowledged run or missing completed output is
blocked. Keep `*.json.lock` files with the journals; child and coordinator updates
use the same lock. Named service Codex configurations preinitialize the screen
reader preference to avoid a first-run UI setting changing frozen config bytes.

Issue development and MCP log acquisition remain independent workflows. There
are no production Issue/tick Sources or Bindings here. Future adapters must
validate exclusive trigger ownership, coalesce pending monitor ticks, report
actual ingestion lag independently, and reconcile their execution journal. They
must not implement an MCP server in this repository.
