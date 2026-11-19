"""Real stdio + HTTP MCP clients against an isolated automatically started server."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import unittest
import urllib.request

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from .core import ROOT


class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_auto_start_queue_operations_and_http_share_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            data=Path(temporary)/'data';image=Path(temporary)/'reference.png'
            Image.new('RGB',(512,512),(130,90,50)).save(image)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            url=f'http://127.0.0.1:{port}'
            environment=dict(os.environ,TRELLIS_STUDIO_DATA=str(data))
            parameters=StdioServerParameters(command=sys.executable,args=['-m','asset_studio','mcp','--port',str(port)],cwd=str(ROOT),env=environment)
            server_pid=None
            async def call(client,name,arguments=None):
                result=await client.call_tool(name,arguments or {})
                self.assertFalse(result.is_error,str(result))
                return result.structured_content or json.loads(result.content[0].text)
            try:
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize();tools=await client.list_tools()
                        self.assertEqual(len(tools.tools),12)
                        status=await call(client,'studio_status');server_pid=status['server_pid']
                        self.assertEqual(status['data_dir'],str(data.resolve()))
                        await call(client,'set_queue_paused',{'paused':True})
                        first=await call(client,'queue_asset',{'name':'MCP first','image_path':str(image)})
                        second=await call(client,'queue_asset',{'name':'MCP second','reference_id':first['reference_id']})
                        await call(client,'reorder_queue',{'ordered_ids':[second['id'],first['id']]})
                        jobs=await call(client,'list_jobs');self.assertEqual(jobs['jobs'][0]['id'],second['id'])
                        self.assertEqual((await call(client,'get_job',{'job_id':first['id']}))['status'],'queued')
                        self.assertEqual((await call(client,'get_job_log',{'job_id':first['id']}))['status'],'queued')
                        await call(client,'cancel_job',{'job_id':first['id']})
                        retried=await call(client,'retry_job',{'job_id':first['id']});self.assertNotEqual(retried['id'],first['id'])
                        assets=await call(client,'list_assets');self.assertEqual(len(assets['assets']),0)
                        profiles=await call(client,'generation_profiles');self.assertEqual(profiles['profiles']['balanced']['quantize'],0)
                        await call(client,'cancel_job',{'job_id':second['id']});await call(client,'cancel_job',{'job_id':retried['id']})
                        await call(client,'set_queue_paused',{'paused':False})
                        resources=await client.list_resources();self.assertTrue(resources.resources)
                # Closing the stdio client leaves the local worker/server running.
                async with streamable_http_client(url+'/mcp/') as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize();status=await call(client,'studio_status')
                        self.assertEqual(status['queued'],0);self.assertEqual(status['server_pid'],server_pid)
                        self.assertEqual(len((await call(client,'list_jobs'))['jobs']),3)
                        self.assertEqual(len((await client.list_tools()).tools),12)
            finally:
                if server_pid:
                    os.kill(server_pid,signal.SIGTERM)
                    for _ in range(100):
                        await asyncio.sleep(.05)
                        try:
                            with urllib.request.urlopen(url+'/api/status',timeout=.1):pass
                        except OSError:break


if __name__=='__main__':unittest.main(verbosity=2)
