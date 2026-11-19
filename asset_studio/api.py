from __future__ import annotations
import base64
import binascii
from contextlib import asynccontextmanager
import dataclasses
import fcntl
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from PIL import Image, ImageOps

from .core import Store, Worker, ROOT, DATA, PROFILES, CATEGORIES, GENERATOR_PYTHON, ENCODER, external_generator_busy
from .mcp_server import create_mcp


class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=100)
    reference_id: str | None = None
    image_path: str | None = None
    category: str = 'Props'
    profile: str = 'balanced'
    seed: int = Field(default=42,ge=0,le=4294967295,strict=True)
    memory_gib: float = Field(default=10.,ge=4,le=10)
    notes: str = Field(default='',max_length=4000)


class ReferenceInput(BaseModel):
    filename: str = Field(default='reference.png',max_length=200)
    data: str = Field(max_length=28_000_000)


class QueueInput(BaseModel):
    paused: bool = Field(strict=True)


class OrderInput(BaseModel):
    ordered_ids: list[str] = Field(max_length=10000)


def create_app(data=DATA, port=8766, worker_factory=Worker):
    store = Store(data)
    worker = worker_factory(store)
    base_url = 'http://127.0.0.1:'+str(port)
    mcp = create_mcp(base_url)
    transport = mcp.streamable_http_app(streamable_http_path='/',json_response=True,stateless_http=True)

    @asynccontextmanager
    async def lifespan(app):
        with (store.data/'server.lock').open('a+') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Asset Studio is already using this data folder.')
            worker.start()
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                worker.stop()

    app = FastAPI(title='Trellis Local Studio',version='1.0.0',lifespan=lifespan)
    app.state.store = store; app.state.worker = worker

    @app.middleware('http')
    async def local_requests(request: Request, call_next):
        # No CORS permission: other websites must not enqueue local GPU work.
        host = request.url.hostname
        origin = request.headers.get('origin')
        if host not in {'localhost','127.0.0.1'}:
            return JSONResponse({'detail':'Local connections only.'},status_code=403)
        if origin and origin.rstrip('/') not in {base_url,'http://localhost:'+str(port)}:
            return JSONResponse({'detail':'Open the studio directly to use these controls.'},status_code=403)
        try:
            if int(request.headers.get('content-length','0')) > 29_000_000:
                return JSONResponse({'detail':'Choose an image smaller than 20 MB.'},status_code=413)
        except ValueError:
            return JSONResponse({'detail':'Invalid request length.'},status_code=400)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail':str(error)},status_code=400)

    @app.get('/api/status')
    def status():
        summary = store.summary()
        active = summary['active_job']
        return dict(service='trellislocalstudio',version='1.0.0',server_pid=os.getpid(),project_root=str(ROOT),data_dir=str(store.data),
            url=base_url,mcp_url=base_url+'/mcp/',**summary,
            generator_ready=GENERATOR_PYTHON.is_file() and (ENCODER/'model.safetensors').is_file(),
            external_generator_busy=external_generator_busy() if not active else False)

    @app.get('/api/profiles')
    def profiles():
        return {'profiles':{key:dataclasses.asdict(value) for key,value in PROFILES.items()},'recommended':'balanced',
                'categories':CATEGORIES,'memory_gib':{'min':4,'max':10,'default':10},
                'note':'Balanced is verified on this Mac. Detail/master may exceed its memory budget. Notes do not condition generation.'}

    @app.post('/api/references',status_code=201)
    def upload(payload: ReferenceInput):
        try: content = base64.b64decode(payload.data,validate=True)
        except (ValueError,binascii.Error) as error: raise ValueError('Could not read the image upload.') from error
        ref = store.add_reference(content,payload.filename)
        return dict(ref,url='/api/references/'+ref['id'])

    @app.get('/api/references/{reference_id}')
    def reference(reference_id: str):
        return FileResponse(store.ref(reference_id)['path'],media_type='image/png')

    @app.get('/api/jobs')
    def jobs(): return {'jobs':store.jobs()}

    @app.post('/api/jobs',status_code=201)
    def enqueue(payload: JobInput): return store.enqueue(**payload.model_dump())

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str): return store.job(job_id)

    @app.get('/api/jobs/{job_id}/log')
    def log(job_id: str): return store.log(job_id)

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id: str): return store.cancel(job_id)

    @app.post('/api/jobs/{job_id}/retry',status_code=201)
    def retry(job_id: str): return store.retry(job_id)

    @app.patch('/api/queue')
    def pause(payload: QueueInput): return store.pause(payload.paused)

    @app.post('/api/queue/order')
    def reorder(payload: OrderInput): return store.reorder(payload.ordered_ids)

    @app.get('/api/assets')
    def assets(): return {'assets':store.assets()}

    @app.get('/api/assets/{asset_id}')
    def asset(asset_id: str): return store.asset(asset_id)

    @app.get('/api/assets/{asset_id}/model')
    def model(asset_id: str):
        row = store.asset(asset_id)
        if not Path(row['model_path']).is_file(): raise HTTPException(404,'Model file is missing.')
        return FileResponse(row['model_path'],media_type='model/gltf-binary',filename=row['name']+'.glb',content_disposition_type='inline')

    @app.get('/api/assets/{asset_id}/poster')
    def poster(asset_id: str):
        row = store.asset(asset_id); source = Path(row['poster_path'] or row['reference_path'])
        if not source.is_file(): raise HTTPException(404,'Preview is missing.')
        folder = store.data/'thumbnails';folder.mkdir(exist_ok=True)
        target = folder/(asset_id+'-'+str(source.stat().st_mtime_ns)+'.jpg')
        if not target.exists():
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((900,650));image.save(target,quality=88)
        return FileResponse(target,media_type='image/jpeg')

    @app.post('/api/assets/{asset_id}/reuse',status_code=201)
    def reuse(asset_id: str):
        row = store.asset(asset_id)
        if not row['reference_path']: raise ValueError('This asset has no reference image.')
        source = Path(row['reference_path']); ref = store.add_reference(source.read_bytes(),source.name)
        return dict(ref,url='/api/references/'+ref['id'])

    app.mount('/mcp',transport)
    static = Path(__file__).with_name('static')
    app.mount('/static',StaticFiles(directory=static),name='static')

    @app.get('/')
    def index(): return FileResponse(static/'index.html',media_type='text/html')

    return app
