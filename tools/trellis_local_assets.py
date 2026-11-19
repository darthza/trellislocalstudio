"""Validate/download exactly the pinned weights required by a local profile."""
from pathlib import Path
import hashlib,os,re,subprocess,time,concurrent.futures,shutil,fcntl

HUB=Path(os.environ.get('HF_HUB_CACHE', Path.home()/'.cache/huggingface/hub'))
MODEL_REV='af44b45f2e35a493886929c6d786e563ec68364d'
LARGE_REV='25e0d31ffbebe4b5a97464dd851910efc3002d96'
DINO_REV='ea8dc2863c51be0a264bab82070e3e8836b02d51'

def required_weights(resolution):
    names=['ss_flow_img_dit_1_3B_64_bf16','shape_dec_next_dc_f16c32_fp16','tex_dec_next_dc_f16c32_fp16',
           'slat_flow_img2shape_dit_1_3B_512_bf16','slat_flow_imgshape2tex_dit_1_3B_512_bf16']
    if resolution>512:names+=['slat_flow_img2shape_dit_1_3B_1024_bf16','slat_flow_imgshape2tex_dit_1_3B_1024_bf16']
    return [('microsoft/TRELLIS.2-4B',MODEL_REV,'ckpts/'+name+'.safetensors') for name in names]+[
        ('microsoft/TRELLIS-image-large',LARGE_REV,'ckpts/ss_dec_conv3d_16l8_fp16.safetensors'),
        ('facebook/dinov3-vitl16-pretrain-lvd1689m',DINO_REV,'model.safetensors')]

def download_public_weight(repo,revision,name):
    """HTTP fallback avoids the stalled Xet transfer observed on this Mac.

    Verify the Hub's LFS SHA-256 before exposing a file in its snapshot.
    No authentication token is passed to curl; this route is public models only.
    """
    if not repo.startswith('microsoft/'):raise RuntimeError('Public downloader cannot handle gated models.')
    url=f'https://huggingface.co/{repo}/resolve/{revision}/{name}?download=true'
    result=subprocess.run(['curl','--head','--silent','--show-error','--fail','--max-time','30',url],capture_output=True,text=True,check=True)
    headers={}
    for line in result.stdout.splitlines():
        if ':' in line:
            key,value=line.split(':',1);headers[key.lower().strip()]=value.strip()
    expected=headers.get('x-linked-etag','').strip('"')
    size=int(headers.get('x-linked-size','0'))
    if not re.fullmatch('[0-9a-f]{64}',expected) or size<1000:raise RuntimeError('Missing trusted LFS hash/size for '+name)
    model_root=HUB/('models--'+repo.replace('/','--'))
    blob=model_root/'blobs'/expected;blob.parent.mkdir(parents=True,exist_ok=True)
    partial=blob.with_suffix('.trellislocalstudio-partial')
    path=model_root/'snapshots'/revision/name
    # Cooperate with an authenticated Hub download already in flight rather
    # than downloading the same multi-gigabyte model twice.
    hub_lock=HUB/'.locks'/model_root.name/(expected+'.lock')
    hub_lock.parent.mkdir(parents=True,exist_ok=True)
    with hub_lock.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.is_file():return path
        if not blob.is_file():
            # Several bounded HTTP ranges avoid the very slow single-stream
            # transfer on this connection. Each piece is resumable; the combined
            # checkpoint is verified against the Hub's SHA-256 before use.
            count=8;chunk=(size+count-1)//count
            pieces=[blob.with_suffix(f'.trellislocalstudio-part-{i:02}') for i in range(count)]
            if partial.exists() and partial.stat().st_size<=chunk and not pieces[0].exists():partial.rename(pieces[0])
            def fetch(index):
                path=pieces[index];start=index*chunk;end=min(size,(index+1)*chunk)-1;expected_length=end-start+1
                for attempt in range(4):
                    existing=path.stat().st_size if path.exists() else 0
                    if existing==expected_length:return
                    if existing>expected_length:raise RuntimeError('Invalid partial checkpoint size')
                    with path.open('ab') as handle:
                        result=subprocess.run(['curl','--http1.1','--location','--fail','--silent','--show-error',
                            '--connect-timeout','30','--max-time','900','--max-filesize',str(expected_length-existing),
                            '--range',f'{start+existing}-{end}',url+f'&trellislocalstudio-range={index}-{attempt}-{time.time_ns()}'],stdout=handle)
                    if result.returncode==0 and path.stat().st_size==expected_length:return
                raise RuntimeError('Checkpoint range download did not complete')
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                pending={pool.submit(fetch,i) for i in range(count)}
                while pending:
                    done,pending=concurrent.futures.wait(pending,timeout=30,return_when=concurrent.futures.FIRST_EXCEPTION)
                    for future in done:future.result()
                    total=sum(p.stat().st_size for p in pieces if p.exists())
                    print(f'  {Path(name).name}: {total/size:.0%} downloaded',flush=True)
            digest=hashlib.sha256();assembling=blob.with_suffix('.trellislocalstudio-assembling')
            with assembling.open('wb') as output:
                for path in pieces:
                    with path.open('rb') as handle:
                        for block in iter(lambda:handle.read(8*1024**2),b''):digest.update(block);output.write(block)
            if assembling.stat().st_size!=size or digest.hexdigest()!=expected:raise RuntimeError('Downloaded checkpoint failed SHA-256/size verification: '+name)
            os.replace(assembling,blob)
            for path in pieces:path.unlink()
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        temporary=path.with_name(path.name+'.trellislocalstudio-link')
        if temporary.is_symlink():temporary.unlink()
        temporary.symlink_to(os.path.relpath(blob,path.parent));os.replace(temporary,path)
    return path

def ensure_weights(resolution,*,download=True):
    receipt=[]
    for repo,revision,name in required_weights(resolution):
        path=HUB/('models--'+repo.replace('/','--'))/'snapshots'/revision/name
        if not path.is_file():
            if not download:raise RuntimeError('Required model weights are missing: '+name)
            print('Preparing model weights:',Path(name).name,flush=True)
            if repo.startswith('microsoft/'):path=download_public_weight(repo,revision,name)
            else:
                from huggingface_hub import hf_hub_download
                path=Path(hf_hub_download(repo,name,revision=revision))
        if path.stat().st_size<1000:raise RuntimeError('Invalid cached model weights: '+str(path))
        receipt.append({'repo':repo,'revision':revision,'file':name,'bytes':path.stat().st_size,'cache_blob':path.resolve().name})
    return receipt
