# MCP exception Issue workflow

The independent `mcp-exception-issue` script consumes an externally provided MCP
log server. It acquires all operational evidence through MCP and all source
through GitHub at the deployed SHA. It never invokes the Issue-fix workflow.

See [incident operations](../../docs/INCIDENT-WORKFLOWS.md) for policy, provider
qualification, scheduling, and recovery. The server is not implemented here.

```bash
./scripts/run.sh mcp-exception-issue --repository owner/repository \
  --monitor production-api --policy /absolute/operator/incident-policy.json
```
