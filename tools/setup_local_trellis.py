"""Keep the pinned inference dependencies outside cloud-managed Documents."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
ENV=Path(os.environ.get('TRELLIS_GENERATOR_PYTHON', str(Path.home()/'.cache/trellislocalstudio/trellis-venv/bin/python'))).parent.parent
LOCK=ROOT/'tools/trellis-local-requirements.txt'

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

if __name__=='__main__':print(ensure_environment())
