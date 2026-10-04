# mcp-exception-issue v1

Collect exceptions and related logs from an external MCP provider, analyze source
at the deployed GitHub revision, and publish evidence-supported, deduplicated
Issues. Run manually or schedule with external cron. Issue remediation is a
separate manual workflow; this monitor never invokes it.

[All workflows and common setup](../../README.md) ·
[Incident operations](../../docs/INCIDENT-WORKFLOWS.md) ·
[MCP provider specification](../../docs/MCP-LOG-SERVER-SPEC.md)

Commands below run from the repository root. Replace repository, monitor, and paths.

## Prepare and install

1. Complete [common setup](../../README.md#setup-and-installation). Install
   [cao_incident_readonly.config.toml](../../config/cao_incident_readonly.config.toml)
   in the server/launcher's `CODEX_HOME` with mode `0600`. Start the server with
   `CAO_MEMORY_ENABLED=false`; admission checks its effective memory setting.
2. Provide an external MCP log server. Install the MCP Python SDK (inspected with
   1.30.0), `httpx`, and `jsonschema` in the CAO execution environment. This
   repository supplies a client and provider contract, not a server deployment.
3. Copy [incident-policy.example.json](../../config/incident-policy.example.json)
   outside Git. Replace placeholders and set the operator-owned policy and token
   files to mode `0600`. Use absolute state/workspace paths with mode `0700` and
   no symlink components. Keep policy, credentials, and state outside model workspaces.
4. Configure the repository's monitor settings below. GitHub needs Contents and
   Issues read access plus Issues write access for publication.

| Policy setting | What to configure |
| --- | --- |
| `state_root` / `workspace_root` | Persistent private state and separate temporary workspaces; overlapping monitors on one host must share a state root |
| `github_token_file` | Private token file, or use GitHub CLI authentication when omitted |
| `mcp_connections` | Operator-fixed HTTPS endpoint/token file or absolute stdio argv/credential files; configured read tools only |
| `schema_digests` | Qualified SHA-256 hashes of advertised tool input schemas; placeholders are not usable |
| `monitors.MONITOR_ID` | Service, environment, connection, source IDs, selected source paths, and collection limits |
| `deployments` / revision metadata | Exact deployed revision from MCP records, qualified revision tool, or unambiguous deployment intervals |
| `related_monitors` | Explicit related scopes in the same repository, if needed |

Follow [provider qualification and policy setup](../../docs/INCIDENT-WORKFLOWS.md#setup)
for schema digest serialization and transport contracts. Docker/fix policy fields
are needed by Issue fix, not by this monitor.

```bash
./scripts/install.sh mcp-exception-issue
make status
```

Installation deploys `workflow.py` and the `exception-triager` agent profile.
It does not install or configure the MCP provider.

Use `gh auth login` as the CAO account, or configure the policy's private
`github_token_file`. Launcher `GH_TOKEN`/`GITHUB_TOKEN` is not forwarded to
CAO 2.5.0 workflow scripts.

## Run manually

```bash
# Analyze the current interval without GitHub publication or operational state changes.
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json --dry-run

# Publish supported Issues and retain the queue/checkpoint.
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json

# Analyze an explicit historical interval without publication.
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json \
  --from 2026-10-01T00:00:00Z --until 2026-10-01T01:00:00Z --dry-run
```

`--from` and `--until` must be supplied together. Historical publication retains
queued incidents without moving the normal watermark. Dry-run still calls MCP,
GitHub reads, and model analysis, and writes an execution journal; it does not
change the operational queue/checkpoint/publication database.

A new monitor starts 15 minutes before the stable cutoff (now minus two minutes).
Later runs overlap by five minutes. Complete collection is required to advance
its checkpoint. The interval is limited to 24 hours per run.

## Schedule with cron

After qualifying a manual run, invoke the same launcher every five minutes:

```cron
*/5 * * * * /absolute/manager/scripts/run.sh mcp-exception-issue --repository owner/repository --monitor production-api --policy /absolute/operator/incident-policy.json >> /absolute/operator/logs/monitor.log 2>&1
```

Replace the absolute paths and prepare an owner-controlled log directory. Cron
must run as the CAO account with the required CLI `PATH`, authentication, CAO home,
port, and `CODEX_HOME`. Keep the CAO server running separately. The wrapper resolves
its own project root, so cron does not need to change directory.

## Check results and evidence

The launcher prints `Execution journal: ...` before submission. Its journal
contains `run_ids`; inspect the run with:

```bash
cao workflow status RUN_ID
cao workflow result RUN_ID --json
```

CAO 2.5.0's retained result omits run-level `output`. This workflow
and its submission launcher do not implement the PR chain's `child_outputs`
fallback; the launcher can report `CAO output identity mismatch` after a
completed run. Preserve the journal and any live script output, inspect retained
workflow evidence and GitHub publication, and resolve the outcome manually
before retrying. Model step output alone does not establish publication.

When final workflow output is available, inspect `output.status` and every entry
in `output.incidents`. Top-level `processed` does not mean every incident produced an Issue.

| Per-incident status | Meaning / next action |
| --- | --- |
| `issue_created` / `issue_reused` | Supported publication; inspect the Issue URL/number |
| `dry-run` | Proposed Issue candidate; nothing published |
| `non_code` | Evidence did not support a code Issue |
| `deferred` | Transient read failure or expected future evidence; bounded retries on subsequent monitor runs |
| `blocked` / `failed` | Inspect evidence, configuration, and CAO step results before explicit retry |
| `reconciling` | Publication outcome is uncertain; preserve state and resume |

The monitor resolves the deployed revision and selected GitHub source before
collecting additional context. Context starts at five minutes on either side of
the event and may expand to 15 then 60 minutes, applying available trace/request/
instance filters. Queries stay within configured service/environment/source
scopes and end no later than the stable cutoff. Limits are 10,000 records, 5 MB
of evidence, and 120 KB model input. Missing source, ambiguous revision,
incomplete coverage, or insufficient evidence blocks unsupported publication.

## Resume or retry

Use the exact printed execution journal after a lost connection or pending write:

```bash
./scripts/run.sh mcp-exception-issue --resume /absolute/operator/state/executions/EXECUTION.json
```

Resume accepts no input overrides. It queries the recorded run before any
resubmission; completed runs with pending publication can reconcile through a
recorded recovery-only run. Failed/cancelled CAO runs require explicit manual
recovery. Keep journals and `monitor.sqlite3` until publication is resolved.

After correcting a terminal incident failure or supplying new evidence:

```bash
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json \
  --retry-incident EVIDENCE_ID --retry-reason "Corrected deployment interval mapping"
```

Use the `evidence_id` from the incident result. Retry flags require both values
and cannot be used with dry-run. Automatic deferrals have bounded 5/10/20-minute
backoff; terminal failures do not retry automatically.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Unknown monitor / invalid policy or permissions | Match the repository and monitor keys; replace placeholders; check private file/directory modes and absolute paths |
| MCP contract/schema mismatch | Requalify the provider against the specification and update reviewed schema digests |
| No deployed SHA or ambiguous deployment | Supply authoritative revision metadata or correct deployment intervals; source must resolve on GitHub |
| `blocked` from coverage/budget/retention | Check actual provider coverage and selected source; missing evidence cannot be replaced by Issue text |
| Memory admission failure | Disable server memory injection and verify the effective setting; changing the launcher alone is insufficient |
| No new Issues | Inspect per-incident statuses, deduplication, and dry-run mode |
| Uncertain Issue POST | Resume retained state; absence in a bounded listing does not authorize another POST |

See [recovery details](../../docs/INCIDENT-WORKFLOWS.md#recovery-and-evidence-limits)
and [common troubleshooting](../../README.md#troubleshooting).
[config.example.json](config.example.json) documents direct CAO inputs; it is not
automatically loaded by the launcher.
