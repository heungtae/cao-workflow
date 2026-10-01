# External MCP Log Server Specification

Status: Proposed provider contract, version `1.0`. No MCP server implementation
is delivered by this repository.

## Ownership and deliverables

An external provider implements, deploys, operates, authenticates, and maintains
the MCP log server and its connections to logging/deployment backends. This
repository defines the required contract and implements only the consuming CAO
workflows, MCP client/adapter, and client-side contract validation.

| Responsibility | Owner |
| --- | --- |
| This specification, canonical payloads, compatibility and acceptance requirements | This repository |
| Workflow-side connection, tool mapping, validation, and bounded evidence collection | This repository |
| MCP server executable/service, hosting, credentials enforcement, and backend queries | External provider |
| Backend access, retention, availability, supported scopes, and deployment metadata | External provider |
| Workflow policy, provider connection bindings, and credential references | Operator |

No MCP server code, server deployment templates, backend collectors, or backend
administration tools are installation resources in this repository. `manifest.json`
continues to inventory CAO Workflow/Profile resources only. A stdio command may
start an externally supplied executable; it is a client connection configuration,
not an instruction to implement or distribute that server here.

The provider supplies endpoint or executable details, supported transports and
protocol versions, tool names and JSON Schemas, authentication setup, allowed
service/environment/source identifiers, query limits, cursor lifetime, retention,
and sanitized example responses. Provider conformance and production availability
are external prerequisites, not guarantees made by the workflows.

## Protocol and compatibility

The initial client is the inspected MCP Python SDK `1.30.0`. The target MCP
protocol revision is `2025-11-25`, which that installed SDK supports. This
application contract's version `1.0` is separate from the MCP protocol revision
and the provider's own software version. Do not require an untested newer MCP
protocol merely because it is the latest published revision.

The provider supports at least one configured transport: stdio or Streamable
HTTP. Use the selected protocol's standard initialization/version negotiation,
tool discovery, and tool calls. The server exposes its tools capability and
publishes valid `inputSchema` definitions. The canonical provider profile also
publishes `outputSchema` and returns its object payload in `structuredContent`;
any accompanying JSON text represents the same data. Authentication credentials
are supplied through the configured transport, never as log-query arguments.

These protocol facilities follow the official
[MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and [transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
The remaining payload and query rules below are this project's provider contract.

The preferred public names are `logs_search_exceptions`, `logs_search_context`,
and `logs_resolve_revision`. Names are not mandatory: an operator-approved
adapter can map native tool names and payloads to the canonical operations.
One native query tool may supply both search operations. Such mappings must
preserve all required semantics, including coverage and pagination; an adapter
cannot fabricate missing evidence or a completeness guarantee.

Canonical payloads identify `contract_version: "1.0"`. A legacy/native server
without that field requires an explicit tested mapping profile bound to its
tool schemas and provider version. The client can label the normalized result
with the mapping contract version, but must not claim the native server declared
it. Revalidate bindings when tool schemas or capabilities change. Incompatible
changes require a new major application contract or a reviewed mapping update.

## Required capabilities

| Operation | Requirement | Behavior |
| --- | --- | --- |
| Search exceptions | Required | Retrieve exception events, including grouped stack traces, for an explicit service/environment/time range |
| Search context | Required | Retrieve application, access, infrastructure, or other configured logs around an occurrence time, with optional correlation/instance filters |
| Resolve revision | Optional | Resolve the deployed source revision of an affected service/instance at an occurrence time |

When revision resolution is unavailable, the workflows use the operator's
deployment interval mapping or explicit revision fields in MCP log records.
Without an unambiguous deployed revision, incident analysis is blocked. The
server supplies revision metadata only; source files are fetched from GitHub by
the workflow. The server does not create Issues, modify code, push, or select
GitHub publication targets.

## Search request schema

Both search operations accept this canonical object. Providers expose equivalent
JSON Schema types and required fields through tool metadata. Required fields
cannot be omitted; optional fields can be absent but do not accept null.

| Field | Type | Requirement and meaning |
| --- | --- | --- |
| `contract_version` | string | Required; exactly `1.0` |
| `service` | nonempty string | Required authorized service identifier |
| `environment` | nonempty string | Required authorized environment identifier |
| `start` | string, date-time | Required RFC 3339 instant with explicit UTC offset |
| `end` | string, date-time | Required RFC 3339 instant; must be later than `start` |
| `limit` | integer | Optional page record limit, 1 through 1,000; default 500, subject to a documented lower provider maximum |
| `source_ids` | array of unique nonempty strings | Optional selection from authorized log sources; absence uses configured sources for this service/environment |
| `filters` | object | Optional equality filters `trace_id`, `request_id`, and `instance_id`, each a nonempty string; supplied filters combine with AND |
| `cursor` | nonempty string | Optional opaque continuation token for this exact query |

Use the half-open event-time interval `[start, end)`. Normalize comparisons to
UTC without discarding timestamp precision. Raw backend DSL, filesystem paths,
credentials, executable commands, and arbitrary backend endpoints are not query
fields. Reject unknown canonical request/filter fields. Native adapter mapping
is responsible for translating these validated inputs to a fixed approved tool
template, not accepting arbitrary expressions from Issue or model content.

Exception search returns observed exception events. The provider documents its
exception detection/grouping rules and any backend limitations. It must not
invent an exception, causal explanation, or stack trace. Context search returns
the original observed records matching the requested scopes and filters.

Example context request:

```json
{
  "contract_version": "1.0",
  "service": "payments-api",
  "environment": "production",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-01T00:10:00Z",
  "limit": 500,
  "source_ids": ["application"],
  "filters": {"trace_id": "example-trace-01"}
}
```

## Search response and log record schema

Canonical successful responses contain all fields in this table. Null is allowed
only where explicitly stated. Other optional record metadata may be absent.

| Field | Type | Meaning |
| --- | --- | --- |
| `contract_version` | string | Exactly `1.0` |
| `query_id` | nonempty string | Identifier shared by all pages of one query snapshot |
| `observed_at` | string, date-time | UTC time at which the query snapshot was established |
| `records` | array of objects | Records on this page, never more than the effective `limit` |
| `next_cursor` | string or null | Opaque next-page token, or null when there are no remaining pages |
| `coverage` | object | Actual queried interval and whether the backend fully covers it |

`coverage` requires `start` and `end` date-time strings, a `complete` boolean,
and `reason`, which is null for complete coverage or a nonempty explanation for
incomplete coverage. `complete` describes backend coverage at the snapshot time,
not whether this individual page is the last page. The client must also exhaust
`next_cursor` before declaring the full result collected. Retention gaps,
partial source failures, query truncation, and silently capped backend results
must never be represented as complete. Coverage is evidence of the queried
snapshot, not a promise that no late records will ever arrive.

| Record field | Type | Requirement and meaning |
| --- | --- | --- |
| `record_id` | nonempty string | Required stable identity within the provider's source; unchanged on replay |
| `source_id` | nonempty string | Required authorized log-source identifier |
| `occurred_at` | string, date-time | Required observed event time with explicit UTC offset |
| `service`, `environment` | nonempty strings | Required identities matching the authorized query scope |
| `message` | string | Required original observed message, subject to documented provider redaction |
| `severity` | string | Optional native or normalized severity; mapping is documented |
| `stack_trace` | string | Optional complete grouped trace; report truncation in coverage if required content is omitted |
| `trace_id`, `request_id`, `instance_id` | nonempty strings | Optional correlation metadata |
| `deployment_sha` | string | Optional 40-character lowercase hexadecimal GitHub commit SHA |
| `deployment_ref` | nonempty string | Optional deployment tag/version reference requiring GitHub resolution |

Treat source identity plus record ID as the durable event identity. A tested
legacy adapter may derive an identity from immutable record fields when the
provider has none, but must document collision/repeated-event limitations.
Unknown provenance or ambiguous grouping cannot be repaired by generating
plausible values. GitHub repository metadata returned by a provider is a hint
checked against operator mapping, never authority to choose another repository.

Example successful final page:

```json
{
  "contract_version": "1.0",
  "query_id": "example-query-01",
  "observed_at": "2026-10-01T00:12:00Z",
  "records": [
    {
      "record_id": "event-001",
      "source_id": "application",
      "occurred_at": "2026-10-01T00:05:00Z",
      "service": "payments-api",
      "environment": "production",
      "message": "Example exception: missing required input",
      "trace_id": "example-trace-01"
    }
  ],
  "next_cursor": null,
  "coverage": {
    "start": "2026-10-01T00:00:00Z",
    "end": "2026-10-01T00:10:00Z",
    "complete": true,
    "reason": null
  }
}
```

An empty successful query returns `records: []`, complete coverage, and a null
cursor. Empty results do not encode authorization failures or retention gaps.

## Pagination, timing, and replay

The provider documents its ordering and stable continuation semantics. The
canonical order is ascending event time, then source ID and record ID for ties.
All pages preserve query ID, snapshot time, effective query scope, and coverage.
Do not silently change filters, authorization scope, or snapshot while continuing
a cursor. Bind the cursor to the original request and authenticated principal;
publish its expiry and return `CURSOR_EXPIRED` when continuation is unavailable.

Queries and retries are read-only. A fresh query may include newly ingested
late records; a continuation preserves the original snapshot. The provider
documents ingestion delay and retention. Optional ingestion-cursor capabilities
require a separate declared mapping, since a search pagination cursor is not
an incremental ingestion checkpoint. The workflow owns its polling interval,
event-time overlap, deduplication, and evidence-availability retry schedule.

MCP `tools/list` pagination is independent of log-result pagination. Neither an
MCP discovery cursor nor a log continuation token may be used as the other's
cursor. Repeated tokens or changed query identity are contract errors.

## Optional deployed revision operation

The request requires `contract_version`, `service`, `environment`, and `at`
(date-time with explicit offset), with optional `instance_id`. No GitHub token
or source checkout path is accepted. The result requires `contract_version`,
`status`, `candidates`, and `reason`.

- `status=resolved`: exactly one candidate with an observed `deployment_sha` or
  resolvable `deployment_ref`; `reason` is null.
- `status=unresolved`: no candidates and a nonempty reason.
- `status=ambiguous`: at least two candidates and a nonempty reason, for example
  multiple active versions without sufficient instance identity.

Each candidate includes service/environment, the revision reference, metadata
source/provenance ID, and its effective deployment interval when known. A
revision interval is half-open; an open-ended deployment uses a null end.
An affected instance must not be assigned a revision by guessing during a
rolling deployment. A tag is resolved to a commit by the GitHub client before
source analysis. Providers not supporting this operation omit its advertised
tool and declare that limitation in the connection handoff.

## Authentication, errors, and limits

The external provider enforces authorization for every service, environment,
source, and cursor operation. Publish supported authentication and credential
rotation procedures separately from examples. Remote deployments use the
provider's authenticated HTTPS endpoint; stdio deployments use an explicitly
configured executable and credential environment. Do not request secrets or
interactive approval through a log tool response. Tool annotations may describe
read-only intent, but client allowlisting and server authorization remain required.

Transport/protocol errors use the negotiated MCP/HTTP error facilities.
Backend query failures use a tool error result with `isError=true`; the canonical
error payload contains `contract_version`, `code`, a sanitized `message`, and
`retryable`, plus optional nonnegative `retry_after_seconds`. A configured native
mapping may normalize equivalent error data. Required categories are:

| Code | Handling |
| --- | --- |
| `INVALID_ARGUMENT`, `UNAUTHORIZED`, `FORBIDDEN`, `UNSUPPORTED_SCOPE` | Correct input or operator authorization; no automatic incident retry |
| `CURSOR_EXPIRED` | Report incomplete collection; restart the affected range only with workflow deduplication/checkpoint protection |
| `RATE_LIMITED`, `TIMEOUT`, `BACKEND_UNAVAILABLE` | Bounded read retry only when `retryable=true`; honor provider retry hints |
| `INCOMPATIBLE_CONTRACT` | Block binding until the version/schema mapping is reviewed |

Partial successful retrieval uses explicit incomplete coverage, not an empty
success or an undocumented truncation. Providers publish maximum query window,
page/response size, rate limits, source retention, and timeout behavior. The
workflow applies its own stricter incident/context budgets independently.
Error details and examples must contain no credentials, raw backend connection
strings, or unrelated personal data. Server-returned content remains untrusted.

## Acceptance and external handoff

Provider conformance is checked with client-side contract fixtures and an
operator-supplied test endpoint/executable. Fixture responses simulate server
data; they are not a production MCP server implementation. Check:

- Supported protocol negotiation, transport/authentication, discovery of mapped
  tools, required schemas, and explicit contract/profile compatibility.
- Exception and context search, UTC/offset conversion, interval boundaries,
  correlation filters, multiline grouping, and stable event identities.
- Multi-page snapshots, tied timestamps, cursor binding/expiry, empty results,
  incomplete coverage, retention gaps, late ingestion, and rate-limit errors.
- Optional deployment resolution, rolling-deployment ambiguity, unsupported
  resolution, and operator-mapping fallback.
- Read-only execution, scope isolation, redaction/provenance, and rejection of
  arbitrary backend commands or credential-bearing query fields.

Tests against an unavailable external server cannot be described as live
integration success. Versioned examples, tool JSON Schemas, semantic rules, and
provider limitations are delivered with the external handoff. This repository
validates the consuming contract; it does not deploy or certify the provider's
logging infrastructure.
