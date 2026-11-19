"""Behavioral tests run in disposable data folders; no inference or user queue changes."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from .api import create_app
from .core import Store, Worker, terminate_group


def image_bytes(size=(512,512)):
    stream=io.BytesIO();Image.new('RGBA',size,(170,120,70,255)).save(stream,format='PNG');return stream.getvalue()


def wait_for(predicate,timeout=10):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        value=predicate()
        if value:return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for test condition.')


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name);self.store=Store(self.folder)
        self.ref=self.store.add_reference(image_bytes(),'test.png')['id']
        self.worker=None

    def tearDown(self):
        if self.worker:self.worker.stop()
        self.temp.cleanup()

    def enqueue(self,name='Test asset',**kwargs):
        return self.store.enqueue(name=name,reference_id=self.ref,**kwargs)

    def test_durable_pause_order_cancel_and_retry(self):
        self.store.pause(True);a=self.enqueue('First');b=self.enqueue('Second');c=self.enqueue('Third')
        self.assertIsNone(self.store.claim());self.store.reorder([c['id'],a['id'],b['id']])
        restarted=Store(self.folder);self.assertTrue(restarted.paused())
        self.assertEqual([x['name'] for x in restarted.jobs()],['Third','First','Second'])
        self.store.cancel(a['id']);retry=self.store.retry(a['id'])
        self.assertNotEqual(retry['id'],a['id']);self.assertEqual(retry['retry_of'],a['id'])
        self.assertEqual(self.store.job(a['id'])['status'],'cancelled')
        self.assertNotEqual(retry['output_dir'],a['output_dir'])
        self.store.pause(False);self.assertEqual(self.store.claim()['id'],c['id'])
        self.assertIsNone(self.store.claim())

    def test_validation_and_stale_reorders(self):
        for payload in [dict(profile='unknown'),dict(seed=True),dict(seed=-1),dict(memory_gib=11),dict(category='shell'),dict(name='   ')]:
            with self.assertRaises(ValueError):self.store.enqueue(reference_id=self.ref,**({'name':'Test'}|payload))
        with self.assertRaises(ValueError):self.store.add_reference(image_bytes((32,32)))
        with self.assertRaises(ValueError):self.store.add_reference(b'not an image')
        a=self.enqueue()
        with self.assertRaises(ValueError):self.store.reorder([a['id'],a['id']])
        self.enqueue('New arrival')
        with self.assertRaises(ValueError):self.store.reorder([a['id']])
        with self.assertRaises(ValueError):self.store.job_dir('../escape')

    def test_atomic_claim_between_competing_workers(self):
        from concurrent.futures import ThreadPoolExecutor
        self.enqueue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed=list(pool.map(lambda _:self.store.claim(),range(8)))
        self.assertEqual(sum(job is not None for job in claimed),1)

    def test_reference_is_preserved_independently(self):
        source=self.folder/'source.png';source.write_bytes(image_bytes())
        job=self.store.enqueue(name='Copy test',image_path=str(source));source.unlink()
        saved=Path(self.store.ref(job['reference_id'])['path'])
        self.assertTrue(saved.is_file());self.assertNotEqual(saved,source)

    def test_worker_completion_and_failure_continue_queue(self):
        # A tiny valid GLB fixture, never presented as a real generated asset.
        document=json.dumps({'asset':{'version':'2.0'},'scenes':[{}],'scene':0}).encode()
        document+=b' '*((-len(document))%4)
        glb=struct.pack('<4sII',b'glTF',2,20+len(document))+struct.pack('<I4s',len(document),b'JSON')+document
        fixture=self.folder/'fixture.glb';fixture.write_bytes(glb)
        script=self.folder/'fixture_worker.py';script.write_text('''
import sys,json,hashlib,time
from pathlib import Path
folder=Path(sys.argv[1]);folder.mkdir(parents=True,exist_ok=True)
if sys.argv[3]=='fail':
 print('Intentional test failure',flush=True);sys.exit(7)
time.sleep(.1)
data=Path(sys.argv[2]).read_bytes();(folder/'model.glb').write_bytes(data)
(folder/'run.json').write_text(json.dumps({'state':'generated','glb_sha256':hashlib.sha256(data).hexdigest(),'triangles':1}))
''')
        failed=self.enqueue('fail');good=self.enqueue('good')
        command=lambda job:[sys.executable,str(script),job['output_dir'],str(fixture),job['name']]
        self.worker=Worker(self.store,command)
        with patch('asset_studio.core.external_generator_busy',return_value=False):
            self.worker.start();wait_for(lambda:self.store.job(good['id'])['status']=='completed')
        self.assertEqual(self.store.job(failed['id'])['status'],'failed')
        self.assertIn('Intentional test failure',self.store.job(failed['id'])['error'])
        self.assertEqual(self.store.asset(good['id'])['sha256'],hashlib.sha256(glb).hexdigest())
        self.assertEqual(self.store.job(good['id'])['result']['triangles'],1)

    def test_cancel_kills_child_group_and_releases_worker(self):
        script=self.folder/'process_tree.py';script.write_text('''
import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)'])
time.sleep(.1)
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(90)
''')
        pid_file=self.folder/'child.pid';job=self.enqueue()
        self.worker=Worker(self.store,lambda job:[sys.executable,str(script),str(pid_file)])
        with patch('asset_studio.core.external_generator_busy',return_value=False):
            self.worker.start();wait_for(pid_file.exists);parent_pid=self.store.job(job['id'])['pid']
            self.store.cancel(job['id']);wait_for(lambda:self.store.job(job['id'])['status']=='cancelled')
        child_pid=int(pid_file.read_text())
        def stopped(pid):
            info=subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
            return not info or info.startswith('Z')
        wait_for(lambda:stopped(child_pid));self.assertTrue(stopped(parent_pid))

    def test_shutdown_interrupts_and_restart_preserves_queue(self):
        first=self.enqueue('Running');second=self.enqueue('Waiting')
        self.worker=Worker(self.store,lambda job:[sys.executable,'-c','import time;time.sleep(90)'])
        with patch('asset_studio.core.external_generator_busy',return_value=False):
            self.worker.start();wait_for(lambda:self.store.job(first['id'])['pid'])
            self.worker.stop()
        restarted=Store(self.folder)
        self.assertEqual(restarted.job(first['id'])['status'],'interrupted')
        self.assertEqual(restarted.job(second['id'])['status'],'queued')
        Worker(restarted).recover()
        self.assertEqual(restarted.job(second['id'])['status'],'queued')

    def test_restart_recovery_kills_only_owned_orphan(self):
        job=self.enqueue();self.store.claim();folder=self.store.job_dir(job['id']);folder.mkdir(parents=True)
        script=folder/'run_local_trellis.py';script.write_text('import time;time.sleep(90)')
        orphan=subprocess.Popen([sys.executable,str(script),'--output-dir',str(folder/'output')],start_new_session=True)
        try:
            self.store.update(job['id'],pid=orphan.pid);Worker(self.store).recover()
            self.assertEqual(self.store.job(job['id'])['status'],'interrupted');orphan.wait(timeout=6)
        finally:terminate_group(orphan)

    def test_http_validation_and_local_browser_boundary(self):
        class NoWorker:
            def __init__(self,store):pass
            def start(self):pass
            def stop(self):pass
        app=create_app(self.folder,port=8766,worker_factory=NoWorker)
        with TestClient(app,base_url='http://127.0.0.1:8766') as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/api/status').json()['service'],'trellislocalstudio')
            self.assertEqual(client.post('/api/jobs',json={'name':'HTTP test','reference_id':self.ref}).status_code,201)
            self.assertEqual(client.post('/api/jobs',json={'name':'Bad seed','reference_id':self.ref,'seed':False}).status_code,422)
            self.assertEqual(client.patch('/api/queue',json={'paused':True},headers={'Origin':'https://unrelated.example'}).status_code,403)
            self.assertEqual(client.get('/api/status',headers={'Host':'unrelated.example'}).status_code,403)
            self.assertEqual(client.get('/api/assets/../../etc/passwd/model').status_code,404)
            self.assertEqual(client.post('/api/jobs',json={'name':'No ref','reference_id':'bad'}).status_code,400)


if __name__=='__main__':unittest.main(verbosity=2)
