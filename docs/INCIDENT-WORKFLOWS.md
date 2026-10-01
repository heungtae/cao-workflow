# Incident workflow operations

Two independent CAO script workflows are included:

| Workflow | Trigger | Result |
| --- | --- | --- |
| `mcp-exception-issue` v1 | Manual interval or external cron | Evidence-supported, deduplicated GitHub Issue |
| `github-issue-fix` v1 | Manually selected open Issue number | Validated patch, or new branch push and result comment |

The MCP server is supplied by an external provider. This repository delivers
the [provider contract](MCP-LOG-SERVER-SPEC.md), consuming client, workflow
resources, and deterministic publishers. No server implementation is installed.

## Setup

Use CAO 2.5.0 or later with the inspected MCP Python SDK 1.30.0, `httpx` and
`jsonschema` in the CAO execution environment. Configure GitHub CLI authentication
for the deterministic process or a private `github_token_file`. Tokens are read
from operator-owned files, never from CAO inputs or model carriers. Install the
named [Codex configuration](../config/cao_incident_readonly.config.toml) as
`cao_incident_readonly.config.toml` in the server's CODEX_HOME with mode `0600`.
Disable CAO server memory injection (`CAO_MEMORY_ENABLED=false`); admission checks
the server's effective `/settings/memory` response before model steps/new writes.

Copy [the policy example](../config/incident-policy.example.json) outside Git.
Replace all placeholders. Policy and token files must be regular owner-only
`0600` files; state/workspace directories must be `0700`, absolute, and without
symlink components. Keep credentials and policies outside model workspaces.
The same persistent `state_root` must be used for all overlapping monitors on
one host. A submission `--state-root` override changes journals only, not shared
incident/publication storage.

Bind the provider's service, environment, source IDs and read tool names. Qualify
canonical input/output schemas against version 1.0 of the provider specification.
Record `schema_digests` using SHA256 of UTF-8 JSON serialized with sorted keys
and separators `(',', ':')` for each advertised `inputSchema`. Changed schemas
block execution until the operator reviews/requalifies the binding. This first
client requires canonical payload semantics even when tool names differ. Native
payload transformations and ingestion-cursor adapters require a separately
qualified future mapping; response completeness is never synthesized.

Streamable HTTP uses an operator-fixed HTTPS URL and optional bearer-token file.
For stdio, configure `transport: "stdio"`, absolute external `argv`, and optional
`env_files` mapping environment names to private credential files. Only the
configured read operations are called. Provider descriptions/tool annotations
cannot change the configured operations or targets.

Supply deployment SHAs/refs in MCP records or configure half-open deployment
intervals using `start`, `end`, `revision`, and optional `instance_id`. Ambiguous
rolling deployments block analysis. An optional `tools.revision` plus qualified
`schema_digests.revision` binding enables the provider's deployed revision tool.
GitHub resolves tags to pinned commit SHAs.
Source context consists of 1..40 operator-configured `source_paths`, with a
40KB limit per file and 120KB model-input budget; enlarge the selected context
by changing policy explicitly when needed. Collection is bounded at 10,000
records/5MB and 24 hours per run. Missing or truncated evidence is not published.

For ordinary code Issues, `allow_code_issues` explicitly permits manual admission
without incident logs. Incident Issues require a verified producer plus replay
metadata, or an operator `issue_incidents` entry keyed by Issue number. That
metadata supplies `service`, `environment`, `occurred_at`, optional `filters`
and optional `deployed_sha`. Incident log excerpts copied into Issues are not
used as evidence; the workflow re-queries MCP and checks authoritative revision
metadata. Set `incident_publishers` to the authenticated monitor publisher.

Issue fixes require `editable_paths`, selected source/test context, designated
`regression_test_paths`, explicitly allowlisted `new_files`, configured Git
author, a preloaded digest-pinned Docker image, and mandatory test argv arrays.
Every fix must edit a designated regression-test path and supply regression
scenarios. Container tests run on disposable copies with no network, host
credentials, `.git`, or candidate-workspace mount. Images must already contain
test dependencies because automatic image pulling/network access is disabled.

```bash
make validate
make test
./scripts/install.sh mcp-exception-issue
./scripts/install.sh github-issue-fix
```

Install/update/uninstall retain manifest ownership/hash checks. These commands
do not deploy or configure an MCP server or automatically change Codex config.

## Execution

```bash
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json

./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json \
  --from 2026-10-01T00:00:00Z --until 2026-10-01T01:00:00Z --dry-run

./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json --apply-mode patch

./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json
```

For periodic monitoring, an operator cron invokes the monitor launcher every
five minutes. Use absolute manager/policy paths and a CAO server running with
memory injection disabled. A new monitor starts 15 minutes before the stable
cutoff (now minus two minutes); later runs overlap by five minutes. Only complete
collection advances the watermark, atomically with deduplicated queued records.
Historical publishing retains the queue without moving the watermark. Dry-run
never mutates the operational queue, checkpoint or publication database.

Context windows expand from 5 to 15 to 60 minutes around the incident. Configure
related same-repository monitoring scopes explicitly with `related_monitors`.
Transient read failures/expected future evidence allow at most three deferred
retries, with 5/10/20-minute backoff. Contract/auth/retention/budget/revision
failures are blocked; terminal model/validation failures are not automatic retries.

The fix launcher defaults to push mode. Patch mode has a distinct execution
identity and never writes GitHub. The branch is derived from the specification,
base SHA, policy and relevant deployed-resource identity. The publisher checks
Issue/base changes and competing branch creation, creates exactly one commit
whose parent is the validated base, and verifies its remote SHA. Verified own
result comments do not create a new specification digest; human edits, forged
markers, and edited own comments remain specification inputs.

## Recovery and evidence limits

Every submission prints a durable journal path before calling CAO. Resume rejects
input overrides and queries the recorded run before submitting. Only a confirmed
unknown-run response permits resubmission with the same ID; transport failures
do not. Failed/cancelled runs require explicit CAO manual recovery. Completed
runs with pending publication allocate a recorded recovery-only child; it never
reruns model steps, tests, or Git pushes. Reconciliation after policy/resource
changes permits reads only and preserves the observed remote result.

```bash
./scripts/run.sh github-issue-fix --resume /absolute/operator/state/executions/EXECUTION.json
./scripts/run.sh mcp-exception-issue --resume /absolute/operator/state/executions/EXECUTION.json
```

After correcting a terminal failure or supplying new evidence, explicitly retry
with a recorded explanation. Monitor retries retain incident attempt history;
fix retries retain the previous execution and use a new attempt identity. Prior
push/publication intents are reconciled first; these flags never repeat an
ambiguous write or override a competing remote branch.

```bash
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json \
  --retry-incident EVIDENCE_ID --retry-reason "Corrected deployment interval mapping"

./scripts/run.sh github-issue-fix --repository owner/repository --issue 123 \
  --policy /absolute/operator/incident-policy.json \
  --retry-reason "Preloaded the required test dependencies"
```

An ambiguous Issue/comment POST is reconciled by exact retained marker, publisher
and body. If no matching remote artifact can be established, the intent remains
unresolved; absence in a bounded listing does not authorize a second POST. An
ambiguous push is checked against the retained commit; no new development or
push is performed by recovery-only execution. Preserve journals, private SQLite
state and candidate artifacts until the operator resolves the outcome.

Fixtures exercise contract validation, editing, deduplication, journals and
publication reconciliation. They do not establish external-provider availability,
real model proposal quality or live GitHub publication. Qualify those separately
using a provider-supplied test server and a disposable GitHub repository.
The optional polling coordinator in the polling design remains a separate future
integration; neither workflow depends on it.

Deployment scripts are generated from `workflows/_incident/*.py`. After editing
fragments, run `python3 scripts/build_incident_workflows.py`; validation rejects
outdated generated resources. Only manifest Workflow/Profile entries are deployed.
