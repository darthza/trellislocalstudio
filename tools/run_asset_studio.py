"""Prepare a separate light environment; never modify the inference environment."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import fcntl

ROOT = Path(__file__).resolve().parents[1]
ENV = Path.home()/'.cache/trellislocalstudio/studio-venv'
LOCK = ROOT/'asset_studio/requirements.txt'


def main():
    fingerprint = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    receipt = ENV/'studio-environment.json'; python = ENV/'bin/python'
    ENV.parent.mkdir(parents=True,exist_ok=True)
    with (ENV.parent/'setup.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous = json.loads(receipt.read_text()) if receipt.exists() else {}
        if not python.is_file() or previous.get('requirements_sha256') != fingerprint:
            uv = shutil.which('uv') or str(Path.home()/'.local/bin/uv')
            if not Path(uv).is_file(): raise RuntimeError('uv is needed to install the asset studio.')
            if not python.is_file():
                subprocess.run([uv,'venv',str(ENV),'--python',sys.executable],check=True,stdout=sys.stderr,stderr=sys.stderr)
            subprocess.run([uv,'pip','install','--python',str(python),'-r',str(LOCK)],check=True,stdout=sys.stderr,stderr=sys.stderr)
            receipt.write_text(json.dumps({'requirements_sha256':fingerprint})+'\n')
    os.chdir(ROOT)
    os.execv(str(python),[str(python),'-m','asset_studio',*sys.argv[1:]])


if __name__ == '__main__': main()
