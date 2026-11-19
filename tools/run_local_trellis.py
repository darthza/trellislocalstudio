"""Memory-bounded local quality generation with preserved masters and receipts."""
from pathlib import Path
import argparse, dataclasses, json, os, shutil, subprocess, sys, time
from trellis_local_runtime import ADAPTER,CACHE,ENCODER,PIN,PROFILES,atomic_json,ensure_snapshot,generation_args,run_worker,sha,validate_cascade_routes,check_memory_budget
ROOT=Path(__file__).resolve().parents[1]

def read_effective_resolution(checkpoints):
    for name in ['mesh_raw.json','shape_slat.json','mesh_clean.json']:
        path=Path(checkpoints)/name
        if path.exists():return int(json.loads(path.read_text())['mesh_grid_size'])
    raise RuntimeError('Generator did not record its effective geometry resolution.')

def main():
    if '--worker' in sys.argv[1:]:
        worker_parser=argparse.ArgumentParser(add_help=False)
        worker_parser.add_argument('--worker',action='store_true')
        worker_parser.add_argument('--memory-gib',type=float,default=10.)
        worker_args,upstream=worker_parser.parse_known_args()
        upstream=upstream[1:] if upstream[:1]==['--'] else upstream
        return run_worker(upstream,memory_gib=worker_args.memory_gib)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',nargs='?',type=Path)
    parser.add_argument('--profile',choices=PROFILES,default='balanced')
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--memory-gib',type=float,default=10.)
    parser.add_argument('--max-tokens',type=int)
    parser.add_argument('--quantize',type=int,choices=[0,4,8])
    parser.add_argument('--texture-size',type=int,choices=[1024,2048,4096])
    parser.add_argument('--target-faces',type=int)
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--preflight-only',action='store_true')
    args=parser.parse_args()
    if not args.image and not args.resume:parser.error('An image or complete checkpoint set is required.')
    if not 4<=args.memory_gib<=10:parser.error('Use a 4–10 GiB MLX limit on this 16 GiB Mac.')
    from setup_local_trellis import ensure_environment,ENV
    local_python=ensure_environment()
    if Path(sys.prefix).resolve()!=ENV.resolve():
        os.execv(str(local_python),[str(local_python),str(Path(__file__).resolve()),*sys.argv[1:]])
    profile=PROFILES[args.profile]
    for key in ['max_tokens','quantize','texture_size','target_faces']:
        value=getattr(args,key)
        if value is not None:profile=dataclasses.replace(profile,**{key:value})
    if profile.target_faces<10000 or profile.max_tokens<1024:parser.error('Invalid face/token budget.')
    from trellis_local_assets import ensure_weights
    weight_receipt=ensure_weights(profile.resolution) if not args.resume else []
    source=ensure_snapshot()
    if not (ENCODER/'model.safetensors').is_file():raise RuntimeError('Approved DINOv3 encoder is not cached.')
    resume=args.resume.resolve() if args.resume else None
    if resume:
        for name in ['mesh_raw.npz','texture.npz','mesh_raw.json']:
            if not (resume/name).is_file():raise RuntimeError('Finishing requires raw geometry and decoded appearance: missing '+name)
        identity={'resume':{name:sha(resume/name) for name in ['mesh_raw.npz','texture.npz','mesh_raw.json']}}
    else:
        image=args.image.expanduser().resolve()
        if not image.is_file():raise FileNotFoundError(image)
        from PIL import Image
        with Image.open(image) as im:
            if min(im.size)<256:raise RuntimeError('Use a detailed single-object reference at least 256 pixels across.')
        identity={'reference_sha256':sha(image),'reference_name':image.name}
    settings={'profile':args.profile,**dataclasses.asdict(profile),'seed':args.seed,
        'memory_limit_gib':args.memory_gib,'adapter':ADAPTER,'adapter_sha256':sha(Path(__file__).with_name('trellis_local_runtime.py')),
        'environment_lock_sha256':sha(ROOT/'tools/trellis-local-requirements.txt'),
        'asset_loader_sha256':sha(Path(__file__).with_name('trellis_local_assets.py')),
        'runner_sha256':sha(__file__),'vendor_revision':PIN,'finishing_only':bool(resume),**identity}
    run_id=sha_bytes(json.dumps(settings,sort_keys=True).encode())[:16]
    name=args.image.stem if args.image else 'refinish'
    folder=(args.output_dir or ROOT/'art/experiments/trellis-quality-2026-11-18/runs'/f'{name}-{args.profile}-seed-{args.seed}-{run_id[:8]}').resolve()
    metadata=folder/'run.json'
    if metadata.exists():
        old=json.loads(metadata.read_text())
        if old['settings']!=settings:raise RuntimeError('Output folder belongs to different settings; choose a new output directory.')
        if old.get('state')=='generated':
            if sha(folder/'model.glb')!=old['glb_sha256']:raise RuntimeError('Generated file changed; preserving it for review.')
            print('Existing verified model retained:',folder/'model.glb');return
    if (folder/'model.glb').exists():raise RuntimeError('Preserving existing model; use a new output folder.')
    folder.mkdir(parents=True,exist_ok=True)
    checkpoints=CACHE/'runs'/run_id/'checkpoints'
    record={'state':'preflight_passed','settings':settings,'checkpoints':str(checkpoints),
        'expected_resolution':read_effective_resolution(resume) if resume else profile.resolution,
        'weights':weight_receipt}
    atomic_json(metadata,record)
    if args.preflight_only:print(json.dumps(record,indent=2));return
    if not resume:shutil.copy2(image,folder/('reference'+image.suffix.lower()))
    upstream=generation_args(profile,None if resume else image,folder/'model.glb',checkpoints,seed=args.seed,resume=resume)
    command=[sys.executable,str(Path(__file__).resolve()),'--worker','--memory-gib',str(args.memory_gib),'--',*upstream]
    started=time.monotonic();record['state']='generating';atomic_json(metadata,record)
    attempt=1
    while (folder/f'attempt-{attempt:02}.log').exists():attempt+=1
    log=folder/f'attempt-{attempt:02}.log';record['log']=log.name
    process=None;keep_awake=None
    try:
        peak_rss=0
        with log.open('w') as handle:
            process=subprocess.Popen(command,stdout=handle,stderr=subprocess.STDOUT,cwd=ROOT)
            if sys.platform=='darwin':
                # Hold only while this worker exists; never change system-wide
                # sleep settings or keep the display awake.
                keep_awake=subprocess.Popen(['/usr/bin/caffeinate','-i','-s','-w',str(process.pid)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            log_position=0
            while process.poll() is None:
                with log.open() as progress:
                    progress.seek(log_position)
                    for line in progress:
                        if line.startswith('==='):print(line.strip('= \n'),flush=True)
                    log_position=progress.tell()
                if time.monotonic()-started>2400:
                    process.terminate();process.wait(timeout=30);raise RuntimeError('Generation exceeded its 40-minute bound; checkpoints retained.')
                result=subprocess.run(['/bin/ps','-o','rss=','-p',str(process.pid)],capture_output=True,text=True)
                if result.stdout.strip():peak_rss=max(peak_rss,int(result.stdout.strip())*1024)
                if peak_rss>12*1024**3:
                    process.terminate();process.wait(timeout=30);raise RuntimeError('Process crossed the 12 GiB memory bound; checkpoints retained. Select balanced explicitly.')
                time.sleep(2)
        if process.returncode:raise RuntimeError(f'Generator exited {process.returncode}; inspect {log}')
        runtime=json.loads((checkpoints/'runtime.json').read_text())
        check_memory_budget(runtime['mlx_peak_gib'],args.memory_gib)
        effective=read_effective_resolution(checkpoints)
        if effective!=record['expected_resolution']:raise RuntimeError('Effective resolution differs from the requested generation; refusing success.')
        if profile.resolution>512 and not resume:
            validate_cascade_routes(json.loads((checkpoints/'conditioning-routes.json').read_text()))
        import numpy as np,trimesh
        scene=trimesh.load(folder/'model.glb',force='scene')
        meshes=list(scene.geometry.values());triangles=sum(len(m.faces) for m in meshes)
        if triangles<100 or not np.isfinite(scene.bounds).all():raise RuntimeError('Generated geometry is empty or nonfinite.')
        if any(m.visual.kind!='texture' for m in meshes):raise RuntimeError('Expected textured geometry.')
        record.update(state='generated',seconds=time.monotonic()-started,effective_resolution=effective,
            triangles=triangles,glb_sha256=sha(folder/'model.glb'),peak_rss_gib=peak_rss/1024**3,
            visual_review='Pending actual multi-view inspection; generation completion is not an art-quality verdict.')
        # Portable master carriers survive later game-mesh changes. Large transient
        # decoder checkpoints stay in the local cache outside cloud-managed Documents.
        master=folder/'master';master.mkdir(exist_ok=True)
        for name in ['mesh_raw.npz','mesh_raw.json','texture.npz','texture.json','shape_slat.json','conditioning.json','conditioning_hr.npz','conditioning_hr.json','conditioning-routes.json','preprocessing.json','conditioning-preview.png','runtime.json']:
            path=checkpoints/name
            if not path.exists() and resume:path=resume/name
            if path.exists():shutil.copy2(path,master/name)
        record['master_sha256']={p.name:sha(p) for p in master.iterdir() if p.is_file()}
        print('Generated local model:',folder/'model.glb',flush=True)
    except BaseException as error:
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        record.update(state='failed',failure=str(error) or type(error).__name__,seconds=time.monotonic()-started)
        raise
    finally:
        if keep_awake is not None and keep_awake.poll() is None:
            keep_awake.terminate();keep_awake.wait(timeout=10)
        atomic_json(metadata,record)

def sha_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()

if __name__=='__main__':
    if '--worker' in sys.argv or '--help' in sys.argv or '-h' in sys.argv:main()
    else:
        import fcntl
        CACHE.mkdir(parents=True,exist_ok=True)
        with (CACHE/'request.lock').open('a+') as request_lock:
            try:fcntl.flock(request_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise SystemExit('A local generation request is already running. Its output and log are preserved; wait for it to finish.')
            main()
