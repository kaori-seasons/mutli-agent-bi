"""MCP protocol lifecycle and authenticated adapter boundary, without cloud calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from integrations import mcp_stdio as mcp


class MCPTests(unittest.TestCase):
    def ready(self, call=None):
        session=mcp.Session(call or (lambda name, args: ({'ok':True},False)))
        reply=session.dispatch({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        self.assertEqual(reply['result']['protocolVersion'],'2025-06-18')
        self.assertIsNone(session.dispatch({'jsonrpc':'2.0','method':'notifications/initialized'}))
        return session

    def call(self, session, name, args): return session.dispatch({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}})

    def test_init_required_and_notifications_are_silent(self):
        session=mcp.Session()
        self.assertEqual(session.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/list'})['error']['code'],-32002)
        self.assertIsNone(session.dispatch({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':8}}))
        self.assertEqual(session.dispatch([])['error']['code'],-32600)

    def test_read_tools_never_expose_approval_or_arbitrary_sql(self):
        result=self.ready().dispatch({'jsonrpc':'2.0','id':'list','method':'tools/list'})['result']
        names={t['name'] for t in result['tools']}
        self.assertEqual(names,set(mcp.TOOLS)); self.assertEqual(len(names),8)
        self.assertFalse(any('publish' in n or 'review' in n or 'resolve' in n for n in names))
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in result['tools']))

    def test_injected_identity_url_sql_and_unknown_tool_rejected_before_backend(self):
        calls=[]; session=self.ready(lambda *args:(calls.append(args) or {},False))
        for key in ('principal','token','url','sql'):
            result=self.call(session,'query_execute',{'plan_id':'a'*32,'idempotency_key':'key',key:'untrusted'})
            self.assertEqual(result['error']['code'],-32602)
        self.assertEqual(self.call(session,'publish',{})['error']['code'],-32602)
        self.assertEqual(self.call(session,'execution_get',{'identity':'../../admin'})['error']['code'],-32602)
        self.assertEqual(calls,[])

    def test_known_tool_returns_structured_result_and_business_error(self):
        session=self.ready(lambda name,args:({'error':{'code':'PLANNER_NOT_ENABLED'}},True))
        result=self.call(session,'query_execute',{'plan_id':'a'*32,'idempotency_key':'k'})['result']
        self.assertTrue(result['isError']); self.assertEqual(json.loads(result['content'][0]['text']),result['structuredContent'])

    def test_backend_exception_is_sanitized(self):
        def fail(*args): raise RuntimeError('secret bearer and driver error')
        result=self.call(self.ready(fail),'analysis_get',{'identity':'a'*32})
        self.assertNotIn('secret',json.dumps(result)); self.assertTrue(result['result']['isError'])

    def test_backend_url_and_private_credential_file_are_validated(self):
        with patch.dict(os.environ,MCP_BACKEND_URL='http://example.com'):
            with self.assertRaises(ValueError): mcp.backend('execution_get',{'identity':'a'*32})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'token';path.write_text('synthetic');path.chmod(0o644)
            with patch.dict(os.environ,MCP_BACKEND_URL='http://127.0.0.1:1',MCP_ACCESS_TOKEN_FILE=str(path)):
                with self.assertRaises(ValueError):mcp.backend('execution_get',{'identity':'a'*32})
        self.assertIsNone(mcp.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_stdio_subprocess_emits_only_protocol_json_and_closes_on_eof(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'future-client','capabilities':{},'clientInfo':{'name':'test'}}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'}, {'jsonrpc':'2.0','id':2,'method':'tools/list'}]
        process=subprocess.run([sys.executable,'-m','integrations.mcp_stdio'],input='\n'.join(json.dumps(m) for m in messages)+'\n',text=True,capture_output=True,timeout=5)
        self.assertEqual(process.returncode,0); lines=process.stdout.splitlines();self.assertEqual(len(lines),2)
        self.assertEqual(json.loads(lines[1])['id'],2);self.assertEqual(process.stderr,'')

    def test_oversized_protocol_input_exits_without_unbounded_buffer(self):
        process=subprocess.run([sys.executable,'-m','integrations.mcp_stdio'],input='x'*(mcp.MAX_LINE+2),text=True,capture_output=True,timeout=5)
        self.assertEqual(process.stdout,'');self.assertEqual(process.returncode,0)


if __name__=='__main__':unittest.main()
