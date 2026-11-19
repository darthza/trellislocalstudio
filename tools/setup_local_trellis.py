"""Keep the pinned inference dependencies outside cloud-managed Documents."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
ENV=Path(os.environ.get('TRELLIS_GENERATOR_PYTHON', str(Path.home()/'.cache/trellislocalstudio/trellis-venv/bin/python'))).parent.parent
LOCK=ROOT/'tools/trellis-local-requirements.txt'
VENDOR=Path(os.environ.get('TRELLIS_VENDOR_DIR', ROOT/'vendor/trellis2mlx')).expanduser()
UPSTREAM='https://github.com/lyonsno/trellis2mlx.git'
PIN='449bfbc011401fe03d684a01f0b1068bfe8f6ceb'

def ensure_upstream_source():
    """Fetch the exact reviewed generator revision into the local vendor folder."""
    git=shutil.which('git')
    if not git:raise RuntimeError('Git is required to download the pinned TRELLIS.2 MLX source.')
    if not VENDOR.exists():
        VENDOR.parent.mkdir(parents=True,exist_ok=True)
        temporary=VENDOR.with_name(VENDOR.name+'.install-'+str(os.getpid()))
        try:
            subprocess.run([git,'clone','--filter=blob:none','--no-checkout',UPSTREAM,str(temporary)],check=True)
            subprocess.run([git,'-C',str(temporary),'checkout','--detach',PIN],check=True)
            temporary.rename(VENDOR)
        except BaseException:
            shutil.rmtree(temporary,ignore_errors=True)
            raise
    try:revision=subprocess.check_output([git,'-C',str(VENDOR),'rev-parse','HEAD'],text=True).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'{VENDOR} exists but is not a valid TRELLIS.2 MLX checkout. Remove it or set TRELLIS_VENDOR_DIR to a clean folder.') from error
    if revision!=PIN:
        raise RuntimeError(f'TRELLIS.2 MLX source at {VENDOR} is {revision[:12]}, expected pinned revision {PIN[:12]}.')
    return VENDOR

def ensure_environment():
    fingerprint=hashlib.sha256(LOCK.read_bytes()).hexdigest()
    receipt=ENV/'trellislocalstudio-environment.json';python=ENV/'bin/python'
    if python.is_file() and receipt.exists() and json.loads(receipt.read_text()).get('lock_sha256')==fingerprint:return python
    uv=shutil.which('uv') or str(Path.home()/'.local/bin/uv')
    if not Path(uv).is_file():raise RuntimeError('uv is required to prepare the local generator environment.')
    ENV.parent.mkdir(parents=True,exist_ok=True)
    if not python.is_file():
        subprocess.run([uv,'venv',str(ENV),'--python',str(Path(os.environ.get('TRELLIS_UPSTREAM_PYTHON', sys.executable)))],check=True)
    print('Preparing the local generator environment…',flush=True)
    subprocess.run([uv,'pip','install','--python',str(python),'-r',str(LOCK)],check=True)
    receipt.write_text(json.dumps({'lock_sha256':fingerprint,'python':str(python)},indent=2)+'\n')
    return python

if __name__=='__main__':
    ensure_upstream_source()
    print(ensure_environment())
