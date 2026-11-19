"""Project-owned corrections around the pinned, unmodified MLX generator."""
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib, json, os, subprocess, sys, tarfile, tempfile

ROOT=Path(__file__).resolve().parents[1]
VENDOR=Path(os.environ.get('TRELLIS_VENDOR_DIR', ROOT/'vendor/trellis2mlx'))
PIN='449bfbc011401fe03d684a01f0b1068bfe8f6ceb'
ADAPTER='trellislocalstudio-quality-v1'
CACHE=Path(os.environ.get('TRELLIS_GENERATOR_CACHE', Path.home()/'.cache/trellislocalstudio'))
ENCODER=Path(os.environ.get('TRELLIS_DINOV3_ENCODER', Path.home()/'.cache/huggingface/hub/models--facebook--dinov3-vitl16-pretrain-lvd1689m/snapshots/ea8dc2863c51be0a264bab82070e3e8836b02d51'))

@dataclass(frozen=True)
class Profile:
    resolution:int
    quantize:int
    target_faces:int
    texture_size:int
    steps:int=12
    max_tokens:int=24576

PROFILES={
    'legacy':Profile(512,4,60000,1024),
    'balanced':Profile(512,0,300000,4096),
    'detail':Profile(768,0,500000,4096),
    'master':Profile(1024,0,750000,4096),
}

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n');os.replace(tmp,path)

def check_cascade_budget(coords,resolution,requested,max_tokens):
    """Never allow the upstream loop to silently downgrade or ignore its cap."""
    if resolution!=requested:
        raise RuntimeError(f'Resolution changed from {requested} to {resolution}; refusing a silent quality downgrade.')
    if len(coords)>max_tokens:
        raise RuntimeError(f'Cascade needs {len(coords):,} tokens at {resolution}, above the explicit {max_tokens:,}-token budget. '
                           'Select the balanced profile explicitly or use a larger-memory machine. No lower-resolution model was substituted.')
    return coords

def generation_args(profile,image,output,checkpoints,*,seed=42,resume=None):
    args=['--output',str(output),'--seed',str(seed),'--resolution',str(profile.resolution),
          '--steps',str(profile.steps),'--quantize',str(profile.quantize),
          '--max-tokens',str(profile.max_tokens),'--target-faces',str(profile.target_faces),
          '--texture-size',str(profile.texture_size),'--simplify-first','--no-hole-fill',
          '--repair-exterior-surface','--save-checkpoints',str(checkpoints)]
    if resume:args+=['--resume',str(resume)]
    else:args+=['--image',str(image)]
    if profile.resolution==512:args+=['--no-cascade']
    return args

def flow_checkpoint(path,*,cascade):
    """Select the matching texture model; the pinned port hardcodes 512."""
    path=Path(path)
    if cascade and path.name=='slat_flow_imgshape2tex_dit_1_3B_512_bf16.safetensors':
        return path.with_name('slat_flow_imgshape2tex_dit_1_3B_1024_bf16.safetensors')
    return path

def conditioning_for_checkpoint(checkpoint,low,high):
    if '_1024_' in Path(checkpoint).name:
        if high is None:raise RuntimeError('High-resolution flow requires 1024 image conditioning.')
        return high
    return low

def validate_cascade_routes(routes):
    expected={
        'ss_flow_img_dit_1_3B_64_bf16.safetensors':(512,1029),
        'slat_flow_img2shape_dit_1_3B_512_bf16.safetensors':(512,1029),
        'slat_flow_img2shape_dit_1_3B_1024_bf16.safetensors':(1024,4101),
        'slat_flow_imgshape2tex_dit_1_3B_1024_bf16.safetensors':(1024,4101),
    }
    actual={route['checkpoint']:(route['image_size'],route['conditioning_tokens']) for route in routes}
    if actual!=expected:raise RuntimeError('Cascade conditioning/weight routes do not match the quality recipe: '+str(actual))

def check_memory_budget(peak_gib,budget_gib):
    # MLX documents set_memory_limit as a guideline, not a hard ceiling.
    if peak_gib>budget_gib:
        raise RuntimeError(f'MLX reached {peak_gib:.2f} GiB, above the explicit {budget_gib:g} GiB budget; '
            'stopping at the saved checkpoint. Select balanced explicitly. The MLX allocator setting is advisory.')

def ensure_snapshot():
    revision=subprocess.check_output(['git','-C',str(VENDOR),'rev-parse','HEAD'],text=True).strip()
    if revision!=PIN:raise RuntimeError('Unexpected vendor revision; review before generating.')
    destination=CACHE/'source'/PIN
    if not (destination/'snapshot.json').exists():
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            temporary=Path(temporary);archive=temporary/'source.tar'
            with archive.open('wb') as handle:
                subprocess.run(['git','-C',str(VENDOR),'archive',PIN,'generate.py','trellmlx'],stdout=handle,check=True)
            unpack=temporary/'unpacked';unpack.mkdir()
            with tarfile.open(archive) as handle:
                for member in handle.getmembers():
                    target=(unpack/member.name).resolve()
                    if not target.is_relative_to(unpack.resolve()) or member.issym() or member.islnk():
                        raise RuntimeError('Unexpected archive member')
                handle.extractall(unpack,filter='data')
            archive.unlink()
            atomic_json(unpack/'snapshot.json',{'revision':PIN,'generate_sha256':sha(unpack/'generate.py')})
            unpack.rename(destination)
    receipt=json.loads((destination/'snapshot.json').read_text())
    if receipt['generate_sha256']!=sha(destination/'generate.py'):raise RuntimeError('Cached generator was modified.')
    return destination

def run_worker(args,*,memory_gib):
    import fcntl, runpy
    CACHE.mkdir(parents=True,exist_ok=True)
    with (CACHE/'generation.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another local TRELLIS generation is running; wait for it to finish.')
        source=ensure_snapshot();sys.path.insert(0,str(source))
        os.environ.setdefault('OMP_NUM_THREADS','4')
        import mlx.core as mx
        import mlx.nn as nn
        import numpy as np
        from functools import partial
        import trellmlx.models.dinov3 as dino
        import trellmlx.models.sparse_structure_flow as sparse
        dino.extract_features=partial(dino.extract_features,weights_path=str(ENCODER))
        original=sparse._source_linear
        def control_linear(x,layer):
            if not isinstance(layer,nn.QuantizedLinear):return original(x,layer)
            dense=mx.dequantize(layer.weight,layer.scales,layer.biases,
                group_size=layer.group_size,bits=layer.bits).astype(mx.float32)
            result=x@np.asarray(dense,dtype=np.float32).T
            if layer.bias is not None:result+=np.asarray(layer.bias.astype(mx.float32))
            return result.astype(np.float32)
        sparse._source_linear=control_linear
        mx.set_memory_limit(int(memory_gib*1024**3));mx.set_cache_limit(256*1024**2)
        namespace=runpy.run_path(str(source/'generate.py'),run_name='trellislocalstudio_vendor')
        globals_=namespace['main'].__globals__
        original_requantize=globals_['_requantize_coords']
        requested=int(args[args.index('--resolution')+1]);limit=int(args[args.index('--max-tokens')+1])
        def requantize(coords,lr_resolution,hr_resolution):
            result=original_requantize(coords,lr_resolution,hr_resolution)
            return check_cascade_budget(result,hr_resolution,requested,limit)
        globals_['_requantize_coords']=requantize
        # Save the exact foreground that DINO sees and reject the upstream
        # no-foreground fallback, rather than generating an accidental backdrop.
        import trellmlx.preprocess as preprocess
        old_preprocess=preprocess.preprocess_image_with_provenance
        checkpoint_dir=Path(args[args.index('--save-checkpoints')+1]);checkpoint_dir.mkdir(parents=True,exist_ok=True)
        import trellmlx.checkpoint as checkpoint
        original_save=checkpoint.save_checkpoint
        def save_checked(*values,**kwargs):
            result=original_save(*values,**kwargs)
            check_memory_budget(float(mx.get_peak_memory())/1024**3,memory_gib)
            return result
        checkpoint.save_checkpoint=save_checked
        def prepare(*values,**kwargs):
            result=old_preprocess(*values,**kwargs)
            if result.provenance.get('fallback'):raise RuntimeError('Foreground extraction failed; use a clean alpha-masked input.')
            result.image.save(checkpoint_dir/'conditioning-preview.png')
            atomic_json(checkpoint_dir/'preprocessing.json',result.provenance)
            return result
        preprocess.preprocess_image_with_provenance=prepare
        routes=[]
        cascade=requested>512 and '--no-cascade' not in args and '--resume' not in args
        if cascade:
            # Extract before vendor main resets the seed, preserving the exact
            # sparse/LR random sequence. Both feature sizes see the same crop.
            import gc
            image_path=args[args.index('--image')+1]
            prepared=prepare(image_path)
            original_prepare=preprocess.preprocess_image_with_provenance
            def cached_prepare(path,*values,**kwargs):
                if Path(path).resolve()==Path(image_path).resolve():return prepared
                return original_prepare(path,*values,**kwargs)
            preprocess.preprocess_image_with_provenance=cached_prepare
            print('=== High-resolution image conditioning (1024) ===',flush=True)
            high=dino.extract_features(str(checkpoint_dir/'conditioning-preview.png'),image_size=1024)
            mx.eval(high)
            check_memory_budget(float(mx.get_peak_memory())/1024**3,memory_gib)
            np.savez(checkpoint_dir/'conditioning_hr.npz',cond=np.asarray(high))
            atomic_json(checkpoint_dir/'conditioning_hr.json',{'image_size':1024,'shape':list(high.shape),
                'feature_sha256':hashlib.sha256(np.asarray(high).tobytes()).hexdigest(),
                'foreground':prepared.provenance['output'],
                'routing':'1024 features for HR shape and HR texture; 512 features for sparse and LR shape'})
            gc.collect();mx.clear_cache()
            import trellmlx.weight_loader as weight_loader
            import trellmlx.samplers as samplers
            original_load=weight_loader.load_weights;original_sample=samplers.flow_euler_sample
            loaded={}
            def load(model,path,*values,**kwargs):
                selected=flow_checkpoint(path,cascade=True)
                result=original_load(model,str(selected),*values,**kwargs)
                loaded[id(model)]=selected
                return result
            def sample(model,noise,cond,neg_cond,*values,**kwargs):
                checkpoint=loaded[id(model)]
                selected=conditioning_for_checkpoint(checkpoint,cond,high)
                routes.append({'checkpoint':checkpoint.name,'conditioning_tokens':int(selected.shape[1]),
                    'image_size':1024 if selected is high else 512})
                atomic_json(checkpoint_dir/'conditioning-routes.json',routes)
                return original_sample(model,noise,selected,mx.zeros_like(selected),*values,**kwargs)
            weight_loader.load_weights=load;samplers.flow_euler_sample=sample
        sys.argv=[str(source/'generate.py'),*args]
        print(f'TrellisLocalStudio {ADAPTER}; MLX limit {memory_gib:g} GiB; strict cascade budget {limit:,}.',flush=True)
        try:namespace['main']()
        finally:
            atomic_json(checkpoint_dir/'runtime.json',{'adapter':ADAPTER,'vendor_revision':PIN,
                'mlx_peak_gib':float(mx.get_peak_memory())/1024**3,'mlx_limit_gib':memory_gib,
                'conditioning_routes':routes})
