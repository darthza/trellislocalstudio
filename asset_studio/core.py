"""Persistent queue and one memory-bounded generator. No inference imports here."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('TRELLIS_STUDIO_DATA', str(Path.home() / '.local/share/trellislocalstudio'))).expanduser()
GENERATOR_PYTHON = Path(os.environ.get('TRELLIS_GENERATOR_PYTHON', str(Path.home() / '.cache/trellislocalstudio/trellis-venv/bin/python'))).expanduser()
GENERATOR_CACHE = Path(os.environ.get('TRELLIS_GENERATOR_CACHE', str(Path.home() / '.cache/trellislocalstudio'))).expanduser()
sys.path.insert(0, str(ROOT / 'tools'))
from trellis_local_runtime import PROFILES, ENCODER  # Only lightweight profile constants.

CATEGORIES = ['Vegetation', 'Buildings', 'Props', 'Characters', 'Creatures', 'Terrain']
Image.MAX_IMAGE_PIXELS = 24_000_000


def now():
    return time.time()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def tail(path, limit=16000):
    try:
        with Path(path).open('rb') as handle:
            handle.seek(max(0, Path(path).stat().st_size - limit))
            return handle.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''


class Store:
    def __init__(self, data=DATA):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'studio.sqlite3'
        with self.connection() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('paused','false');
                CREATE TABLE IF NOT EXISTS refs (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, filename TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, notes TEXT NOT NULL,
                    reference_id TEXT NOT NULL REFERENCES refs(id), profile TEXT NOT NULL, seed INTEGER NOT NULL,
                    memory_gib REAL NOT NULL, status TEXT NOT NULL, position INTEGER NOT NULL,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL, pid INTEGER,
                    stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', retry_of TEXT);
                CREATE INDEX IF NOT EXISTS queue_order ON jobs(status,position);
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, model_path TEXT NOT NULL,
                    reference_path TEXT, poster_path TEXT, job_id TEXT UNIQUE, created_at REAL NOT NULL,
                    notes TEXT NOT NULL DEFAULT '', triangles INTEGER, sha256 TEXT);
            ''')

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.db, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def ref(self, ref_id):
        with self.connection() as con:
            row = con.execute('SELECT * FROM refs WHERE id=?', (ref_id,)).fetchone()
        if row is None:
            raise ValueError('Reference image was not found.')
        return dict(row)

    def add_reference(self, content, filename='reference.png'):
        if len(content) > 20 * 1024**2:
            raise ValueError('Choose an image smaller than 20 MB.')
        try:
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError('Choose an image below 24 megapixels.')
                if min(source.size) < 256:
                    raise ValueError('The image must be at least 256 pixels on each side.')
                image = ImageOps.exif_transpose(source).convert('RGBA')
                image.load()
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError('Choose a valid PNG, JPEG or WebP reference image.') from error
        ref_id = uuid.uuid4().hex
        folder = self.data / 'references'; folder.mkdir(exist_ok=True)
        path = folder / (ref_id + '.png'); image.save(path)
        with self.connection(write=True) as con:
            con.execute('INSERT INTO refs VALUES (?,?,?,?,?,?)',
                        (ref_id, str(path), Path(filename).name[:160], image.width, image.height, now()))
        return self.ref(ref_id)

    def enqueue(self, *, name, reference_id=None, image_path=None, category='Props', profile='balanced', seed=42, memory_gib=10., notes='', retry_of=None):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('Give the asset a name between 1 and 100 characters.')
        if category not in CATEGORIES or profile not in PROFILES:
            raise ValueError('Choose a listed category and quality profile.')
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError('Seed must be a whole number between 0 and 4294967295.')
        if not 4 <= memory_gib <= 10:
            raise ValueError('Use a 4–10 GiB memory limit on this Mac.')
        if len(notes) > 4000:
            raise ValueError('Keep notes below 4,000 characters.')
        if image_path:
            path = Path(image_path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > 20 * 1024**2:
                raise ValueError('Reference image is missing or larger than 20 MB.')
            reference_id = self.add_reference(path.read_bytes(), path.name)['id']
        self.ref(reference_id)
        job_id = uuid.uuid4().hex
        with self.connection(write=True) as con:
            position = con.execute('SELECT COALESCE(MAX(position),0)+1 FROM jobs').fetchone()[0]
            con.execute('''INSERT INTO jobs
                (id,name,category,notes,reference_id,profile,seed,memory_gib,status,position,created_at,retry_of)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id,name,category,notes,reference_id,profile,seed,memory_gib,'queued',position,now(),retry_of))
        return self.job(job_id)

    def job_dir(self, job_id):
        # Only stored IDs may resolve output paths.
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('Invalid job identifier.')
        return self.data / 'jobs' / job_id

    def job(self, job_id, include_receipt=True):
        with self.connection() as con:
            row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Job was not found.')
        result = dict(row)
        result['reference_url'] = '/api/references/' + result['reference_id']
        result['output_dir'] = str(self.job_dir(job_id) / 'output')
        if result['status'] == 'completed':
            if include_receipt:
                receipt = read_json(Path(result['output_dir']) / 'run.json')
                result['result'] = {k:receipt.get(k) for k in ('seconds','triangles','glb_sha256','effective_resolution','peak_rss_gib','visual_review')}
            result['model_url'] = '/api/assets/' + job_id + '/model'
        return result

    def jobs(self):
        with self.connection() as con:
            ids = [row[0] for row in con.execute('SELECT id FROM jobs ORDER BY position,created_at')]
        return [self.job(job_id,include_receipt=False) for job_id in ids]

    def summary(self):
        with self.connection() as con:
            counts = dict(con.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status'))
            active = con.execute("SELECT id FROM jobs WHERE status IN ('running','cancelling') LIMIT 1").fetchone()
        return {'active_job':self.job(active[0],include_receipt=False) if active else None,
                'queued':counts.get('queued',0),'completed':counts.get('completed',0),'paused':self.paused()}

    def update(self, job_id, **values):
        allowed = {'status','started_at','finished_at','pid','stage','error'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid update.')
        with self.connection(write=True) as con:
            con.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(),job_id))

    def paused(self):
        with self.connection() as con:
            return con.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == 'true'

    def pause(self, value):
        with self.connection(write=True) as con:
            con.execute("UPDATE settings SET value=? WHERE key='paused'", ('true' if value else 'false',))
        return {'paused': value}

    def claim(self):
        with self.connection(write=True) as con:
            if con.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == 'true':
                return None
            if con.execute("SELECT id FROM jobs WHERE status IN ('running','cancelling')").fetchone():
                return None
            row = con.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY position LIMIT 1").fetchone()
            if row is None:
                return None
            con.execute("UPDATE jobs SET status='running',started_at=?,stage='Starting generator' WHERE id=?", (now(),row[0]))
        return self.job(row[0])

    def cancel(self, job_id):
        self.job(job_id)
        with self.connection(write=True) as con:
            state = con.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            if state == 'queued':
                con.execute("UPDATE jobs SET status='cancelled',finished_at=?,stage='Cancelled before generation' WHERE id=?", (now(),job_id))
            elif state == 'running':
                con.execute("UPDATE jobs SET status='cancelling',stage='Stopping generator' WHERE id=?", (job_id,))
        return self.job(job_id)

    def retry(self, job_id):
        old = self.job(job_id)
        if old['status'] not in {'failed','cancelled','interrupted'}:
            raise ValueError('Only failed, cancelled or interrupted jobs can be retried.')
        return self.enqueue(**{k:old[k] for k in ('name','reference_id','category','profile','seed','memory_gib','notes')}, retry_of=job_id)

    def reorder(self, ids):
        with self.connection(write=True) as con:
            queued = {row[0] for row in con.execute("SELECT id FROM jobs WHERE status='queued'")}
            if len(ids) != len(set(ids)) or set(ids) != queued:
                raise ValueError('The queue changed. Refresh and try reordering again.')
            for position, job_id in enumerate(ids):
                con.execute('UPDATE jobs SET position=? WHERE id=?', (position,job_id))
        return {'ordered_ids': ids}

    def assets(self):
        with self.connection() as con:
            rows = con.execute('SELECT * FROM assets ORDER BY created_at DESC').fetchall()
        return [dict(row, model_url='/api/assets/'+row['id']+'/model', poster_url='/api/assets/'+row['id']+'/poster') for row in rows]

    def asset(self, asset_id):
        for row in self.assets():
            if row['id'] == asset_id:
                return row
        raise ValueError('Asset was not found.')

    def complete(self, job, receipt):
        reference = self.ref(job['reference_id'])['path']
        with self.connection(write=True) as con:
            con.execute('INSERT INTO assets (id,name,category,model_path,reference_path,poster_path,job_id,created_at,notes,triangles,sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (job['id'],job['name'],job['category'],str(self.job_dir(job['id'])/'output/model.glb'),reference,reference,
                 job['id'],now(),job['notes'],receipt.get('triangles'),receipt.get('glb_sha256')))
            con.execute("UPDATE jobs SET status='completed',finished_at=?,pid=NULL,stage='Ready for review' WHERE id=?", (now(),job['id']))

    def log(self, job_id):
        job = self.job(job_id)
        folder = self.job_dir(job_id)
        logs = sorted((folder/'output').glob('attempt-*.log'))
        return {'job_id':job_id,'status':job['status'],'text':tail(folder/'worker.log',8000)+'\n'+(tail(logs[-1],24000) if logs else '')}


def external_generator_busy():
    path = GENERATOR_CACHE / 'request.lock'
    if not path.parent.exists():
        return False
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def group_alive(group_id):
    # macOS can return EPERM for killpg(..., 0) on a dead/zombie group.
    rows = subprocess.run(['/bin/ps','-axo','pgid=,stat='],capture_output=True,text=True,check=True).stdout.splitlines()
    return any(len(parts)==2 and parts[0]==str(group_id) and not parts[1].startswith('Z')
               for parts in (row.split() for row in rows))


def terminate_group(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    # A child can ignore SIGTERM or outlive a parent that failed on its own.
    if group_alive(process.pid):
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass


class Worker:
    def __init__(self, store, command_factory=None):
        self.store = store
        self.command_factory = command_factory or self.command
        self.stopping = threading.Event()
        self.thread = None
        self.process = None

    def command(self, job):
        if not GENERATOR_PYTHON.is_file():
            raise RuntimeError('Install the local TRELLIS generator before starting a job.')
        return [str(GENERATOR_PYTHON),'-u',str(ROOT/'tools/run_local_trellis.py'),self.store.ref(job['reference_id'])['path'],
                '--profile',job['profile'],'--seed',str(job['seed']),'--memory-gib',str(job['memory_gib']),
                '--output-dir',str(self.store.job_dir(job['id'])/'output')]

    def recover(self):
        for job in self.store.jobs():
            if job['status'] not in {'running','cancelling'}:
                continue
            # Clean up only our own recognizable orphan process group, never an unrelated PID.
            if job['pid']:
                result = subprocess.run(['/bin/ps','-p',str(job['pid']),'-o','command='], capture_output=True, text=True)
                if 'run_local_trellis.py' in result.stdout and str(self.store.job_dir(job['id'])) in result.stdout:
                    try:
                        os.killpg(job['pid'], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    for _ in range(40):
                        if not group_alive(job['pid']):
                            break
                        time.sleep(.1)
                    else:
                        try: os.killpg(job['pid'], signal.SIGKILL)
                        except ProcessLookupError: pass
            self.store.update(job['id'],status='interrupted',pid=None,finished_at=now(),stage='Interrupted',
                              error='Studio stopped during generation. Partial files are preserved; retry creates a new output.')

    def start(self):
        self.recover()
        self.thread = threading.Thread(target=self.loop, name='asset-generator', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=20)

    def loop(self):
        while not self.stopping.is_set():
            try:
                if not external_generator_busy():
                    job = self.store.claim()
                    if job:
                        self.run_job(job)
            except Exception as error:
                print('Asset worker:', str(error), file=sys.stderr, flush=True)
            self.stopping.wait(.5)

    def run_job(self, job):
        folder = self.store.job_dir(job['id']); folder.mkdir(parents=True, exist_ok=True)
        process = None
        try:
            with (folder/'worker.log').open('w') as log:
                process = subprocess.Popen(self.command_factory(job), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                self.process = process
                self.store.update(job['id'],pid=process.pid)
                stage = ''
                while process.poll() is None:
                    state = self.store.job(job['id'])['status']
                    if self.stopping.is_set() or state == 'cancelling':
                        terminate_group(process)
                        cancelled = state == 'cancelling'
                        self.store.update(job['id'],status='cancelled' if cancelled else 'interrupted',pid=None,finished_at=now(),
                                          stage='Cancelled' if cancelled else 'Interrupted',error='' if cancelled else 'Studio stopped. Retry when ready.')
                        return
                    lines = tail(folder/'worker.log',4000).splitlines()
                    latest = next((line.strip() for line in reversed(lines) if line.strip()), '')
                    if latest and latest != stage:
                        stage = latest[:180]; self.store.update(job['id'],stage=stage)
                    self.stopping.wait(.5)
            if self.store.job(job['id'])['status'] == 'cancelling':
                self.store.update(job['id'],status='cancelled',pid=None,finished_at=now(),stage='Cancelled')
                return
            if process.returncode:
                text = tail(folder/'worker.log')
                if 'already running' in text:
                    self.store.update(job['id'],status='queued',started_at=None,pid=None,stage='Waiting for the local generator')
                    return
                raise RuntimeError(text.strip()[-1800:] or 'Generator exited with code '+str(process.returncode))
            receipt = read_json(folder/'output/run.json'); model = folder/'output/model.glb'
            if receipt.get('state') != 'generated' or not model.is_file():
                raise RuntimeError('Generator did not produce a verified model. Partial files are preserved.')
            digest = hashlib.sha256()
            with model.open('rb') as handle:
                if handle.read(4) != b'glTF':
                    raise RuntimeError('Output is not a GLB model.')
                handle.seek(0)
                for block in iter(lambda:handle.read(1024**2),b''): digest.update(block)
            if digest.hexdigest() != receipt.get('glb_sha256'):
                raise RuntimeError('Output checksum differs from the generator receipt.')
            self.store.complete(job, receipt)
        except Exception as error:
            if process is not None:
                terminate_group(process)
            self.store.update(job['id'],status='failed',pid=None,finished_at=now(),stage='Needs attention',error=str(error))
        finally:
            self.process = None
