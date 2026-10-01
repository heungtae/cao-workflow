"""Client fixtures using the installed SDK; no server implementation is created."""
import contextlib
import copy
import types
import unittest
from unittest.mock import patch

from test_incidents import monitor, request, response

try:
    import mcp
    import jsonschema
    SDK = True
except ImportError:
    SDK = False


@unittest.skipUnless(SDK, 'Executed separately in the installed CAO SDK environment')
class SDKClientTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, payloads, list_repeat=False, bad_schema=False, budget=10000):
        schema = {'type': 'object', 'required': ['contract_version', 'service', 'environment', 'start', 'end'],
                  'properties': {key: {} for key in request()}, 'additionalProperties': True}
        connection = {'id': 'external', 'transport': 'stdio', 'argv': ['/external/provider-server'],
                      'tools': {'context': 'provider_read_logs'}, 'schema_digests': {'context': monitor.digest(schema)}}
        if bad_schema:
            connection['schema_digests']['context'] = 'wrong'
        calls = []
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def initialize(self):
                pass
            async def list_tools(self, cursor=None):
                return types.SimpleNamespace(tools=[types.SimpleNamespace(name='provider_read_logs', inputSchema=schema)], nextCursor='repeat' if list_repeat else None)
            async def call_tool(self, name, args):
                calls.append(copy.deepcopy(args))
                payload = copy.deepcopy(payloads[min(len(calls) - 1, len(payloads) - 1)])
                error = payload.pop('_is_error', False)
                return types.SimpleNamespace(isError=error, structuredContent=payload, content=[])
        @contextlib.asynccontextmanager
        async def transport(params, **kwargs):
            self.assertEqual('/external/provider-server', params.command)
            self.assertEqual({'PATH'}, set(params.env))
            yield (None, None)
        with patch('mcp.ClientSession', side_effect=lambda *a: Session()), patch('mcp.client.stdio.stdio_client', transport):
            rows = await monitor.collect_async(connection, 'context', request(), max_records=budget)
        return rows, calls

    async def test_complete_pages_share_snapshot_and_retain_provenance(self):
        first, last = response(), response()
        first['next_cursor'] = 'page2'
        last['records'][0]['record_id'] = '2'
        rows, calls = await self.execute([first, last])
        self.assertEqual(2, len(rows))
        self.assertEqual('page2', calls[1]['cursor'])
        self.assertEqual('external', rows[0]['provenance']['server_id'])
        self.assertEqual('provider_read_logs', rows[0]['provenance']['tool'])

    async def test_repeated_cursor_and_changed_snapshot_are_rejected(self):
        first, last = response(), response()
        first['next_cursor'] = last['next_cursor'] = 'repeat'
        with self.assertRaises(monitor.Blocked):
            await self.execute([first, last])
        last['query_id'] = 'different'
        with self.assertRaises(monitor.Blocked):
            await self.execute([first, last])

    async def test_discovery_loop_schema_change_and_record_budget_fail_closed(self):
        for kwargs in ({'list_repeat': True}, {'bad_schema': True}, {'budget': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(monitor.Blocked):
                await self.execute([response()], **kwargs)

    async def test_only_transient_error_categories_retry_and_retain_server_hint(self):
        for code, expected in [('RATE_LIMITED', True), ('UNAUTHORIZED', False), ('CURSOR_EXPIRED', False)]:
            payload = {'_is_error': True, 'contract_version': '1.0', 'code': code, 'message': 'Sanitized failure',
                       'retryable': True, 'retry_after_seconds': 600}
            with self.assertRaises(monitor.ReadFailure) as failure:
                await self.execute([payload])
            self.assertEqual(expected, failure.exception.retryable)
            self.assertEqual(600, failure.exception.retry_after_seconds)


if __name__ == '__main__':
    unittest.main()
