"""Interactive macOS setup assistant for Trellis Local Studio."""
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / '.cache/trellislocalstudio'
GENERATOR_PYTHON = CACHE / 'trellis-venv/bin/python'
MODEL_REPOS = (
    'microsoft/TRELLIS.2-4B',
    'microsoft/TRELLIS-image-large',
    'facebook/dinov3-vitl16-pretrain-lvd1689m',
)
HF_ACCESS_URL = 'https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m'


class TerminalUI:
    def __init__(self):
        self.color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
        self.width = max(58, min(shutil.get_terminal_size((84, 24)).columns, 96))
        self.step = 0
        self.total = 6

    def paint(self, value, code):
        return '\033[' + code + 'm' + value + '\033[0m' if self.color else value

    def line(self, value=''):
        print(value)

    def rule(self):
        self.line(self.paint('─' * self.width, '90'))

    def banner(self, subtitle='Guided installation'):
        self.line()
        self.line(self.paint('  TRELLIS LOCAL STUDIO', '1;37'))
        self.line(self.paint('  ' + subtitle, '90'))
        self.rule()

    def heading(self, title, detail=None):
        self.line()
        self.line(self.paint('  ' + title, '1;37'))
        if detail:
            self.line(self.paint('  ' + detail, '90'))
        self.rule()

    def stage(self, title, detail=None):
        self.step += 1
        self.line()
        self.line(self.paint('  [{}/{}] {}'.format(self.step, self.total, title), '1;36'))
        if detail:
            self.line('  ' + detail)

    def ok(self, message):
        self.line(self.paint('  ✓ ', '32') + message)

    def warn(self, message):
        self.line(self.paint('  ! ', '33') + message)

    def fail(self, message):
        self.line(self.paint('  × ', '31') + message)

    def ask(self, prompt, default=True):
        hint = '[Y/n]' if default else '[y/N]'
        while True:
            try:
                answer = input(self.paint('  ' + prompt + ' ' + hint + ' ', '1;37')).strip().lower()
            except EOFError:
                return False
            if not answer:
                return default
            if answer in ('y', 'yes'):
                return True
            if answer in ('n', 'no'):
                return False
            self.line('  Enter y or n.')

    def pause(self, message='Press Enter to continue.'):
        try:
            input(self.paint('\n  ' + message + ' ', '90'))
        except EOFError:
            pass


UI = TerminalUI()


def locate(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin'):
        for name in names:
            path = Path(candidate) / name
            if path.is_file() and os.access(str(path), os.X_OK):
                return str(path)
    return None


def run(args, env=None, description=None):
    UI.line()
    if description:
        UI.line('  ' + description)
    UI.line(UI.paint('  $ ' + ' '.join(str(part) for part in args), '90'))
    try:
        return subprocess.run([str(part) for part in args], cwd=str(ROOT), env=env, check=False).returncode == 0
    except KeyboardInterrupt:
        UI.warn('Stopped. Setup files already completed are kept; run this installer again to resume.')
        raise


def system_check():
    UI.stage('Check this Mac', 'TRELLIS.2 MLX runs on Apple Silicon.')
    if platform.system() != 'Darwin' or platform.machine() not in ('arm64', 'aarch64'):
        UI.fail('This installer requires macOS on Apple Silicon.')
        UI.line('  The application interface can run elsewhere, but local MLX generation cannot.')
        return False
    memory = None
    try:
        value = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.memsize'], text=True).strip()
        memory = int(value) / 1024 ** 3
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    free = shutil.disk_usage(Path.home()).free / 1024 ** 3
    UI.ok('Apple Silicon Mac · macOS {}'.format(platform.mac_ver()[0] or 'version unknown'))
    if memory is not None:
        UI.line('  Unified memory: {:.0f} GiB'.format(memory))
        if memory < 16:
            UI.warn('16 GiB is the minimum target. Generation may fail or run out of memory.')
            if not UI.ask('Continue with setup anyway?', default=False):
                return False
    UI.line('  Free storage: {:.1f} GiB'.format(free))
    if free < 40:
        UI.warn('Weights, dependencies, and outputs can use tens of gigabytes.')
        if not UI.ask('Continue with setup anyway?', default=False):
            return False
    return True


def install_tools():
    UI.stage('Install required tools', 'Python 3.11, uv, and Git are used by the local generator.')
    brew = locate('brew')
    if not brew:
        UI.warn('Homebrew was not found. It is used to install the required command-line tools.')
        UI.line('  Homebrew runs its own checks and asks before changing the system.')
        if UI.ask('Run the official Homebrew installer now? It may request your Mac password.', default=False):
            command = '/bin/bash -c "$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            if not run(['/bin/bash', '-c', command], description='Running the official Homebrew installer.'):
                UI.fail('Homebrew installation did not finish. Rerun this installer after resolving the message above.')
                return None
            brew = locate('brew')
        else:
            UI.line('  Install Homebrew from https://brew.sh, then rerun this installer.')
            if UI.ask('Open the Homebrew installation page?', default=True):
                webbrowser.open('https://brew.sh')
            return None
        if not brew:
            UI.fail('Homebrew was not found after installation. Close and reopen Terminal, then rerun setup.')
            return None

    python = locate('python3.11')
    uv = locate('uv')
    git = locate('git')
    missing = []
    if not python:
        missing.append('python@3.11')
    if not uv:
        missing.append('uv')
    if not git:
        missing.append('git')
    if missing:
        UI.line('  Missing: ' + ', '.join(missing))
        if not UI.ask('Install these packages with Homebrew?', default=True):
            return None
        if not run([brew, 'install'] + missing, description='Installing prerequisites. This may take a few minutes.'):
            UI.fail('Homebrew could not install the required tools. Fix the message above, then rerun setup.')
            return None
        python = locate('python3.11')
        uv = locate('uv')
        git = locate('git')
    if not (python and uv and git):
        UI.fail('Python 3.11, uv, or Git is still missing. Rerun setup after fixing the tools.')
        return None
    UI.ok('Python 3.11: ' + python)
    UI.ok('uv: ' + uv)
    UI.ok('Git: ' + git)
    return python


def setup_app_environment(python, env):
    UI.stage('Install the studio', 'This installs the lightweight web app and MCP dependencies.')
    if not run([python, 'tools/run_asset_studio.py', 'setup'], env,
               'Preparing the isolated studio environment.'):
        UI.fail('Studio setup did not finish. Correct the error and rerun the installer.')
        return False
    UI.ok('Studio environment is ready.')
    return True


def setup_generator(python, env):
    UI.stage('Install the local generator', 'The pinned TRELLIS.2 MLX source and its Python environment are installed separately.')
    if not run([python, 'tools/setup_local_trellis.py'], env,
               'Downloading the pinned source and preparing the MLX environment. This is a large install.'):
        UI.fail('Generator setup did not finish. Correct the error and rerun; completed downloads are reused.')
        return False
    UI.ok('Pinned generator source and environment are ready.')
    return True


def huggingface_setup(env):
    UI.stage('Connect Hugging Face', 'DINOv3 access must be approved on Hugging Face before its model can download.')
    UI.line('  Access page: ' + HF_ACCESS_URL)
    if UI.ask('Open the access page?', default=False):
        webbrowser.open(HF_ACCESS_URL)
    approved = UI.ask('Has Hugging Face approved your DINOv3 access?', default=False)
    if not approved:
        UI.warn('Skipping model downloads. You can finish access later and rerun this installer.')
        return False

    hf = str(GENERATOR_PYTHON.parent / 'hf')
    if not Path(hf).is_file():
        UI.fail('The Hugging Face command was not installed in the generator environment.')
        return False
    identity = subprocess.run([hf, 'auth', 'whoami'], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if identity.returncode == 0:
        lines = identity.stdout.strip().splitlines()
        UI.ok('Hugging Face is signed in' + (': ' + lines[0] if lines else '.'))
    else:
        UI.line('  Sign in locally. The token is entered into the Hugging Face prompt and is not saved in this project.')
        if not run([hf, 'auth', 'login'], env):
            UI.warn('Authentication was skipped. Model downloads are not available yet.')
            return False
    identity = subprocess.run([hf, 'auth', 'whoami'], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if identity.returncode != 0:
        UI.warn('Hugging Face sign-in was not confirmed. You can authenticate and rerun setup later.')
        return False

    UI.line('  Model repositories:')
    for repo in MODEL_REPOS:
        UI.line('   · ' + repo)
    UI.warn('Model weights are large. Files already in the Hugging Face cache are reused.')
    if not UI.ask('Download the model weights now?', default=True):
        UI.warn('Model downloads skipped. The first generation will need these weights.')
        return False
    for repo in MODEL_REPOS:
        if not run([hf, 'download', repo], env, 'Downloading ' + repo):
            UI.warn('This download did not finish. Rerun setup later; completed model files are cached.')
            return False
    UI.ok('All model weights are available locally.')
    return True


def launch(python, env, models_ready):
    UI.stage('Launch the studio', 'The local web interface will open in your browser.')
    if not UI.ask('Launch Trellis Local Studio now?', default=True):
        UI.ok('Setup is complete. Start it later with ./run-asset-studio.command')
        return
    if run([python, 'tools/run_asset_studio.py', 'open'], env,
           'Starting the local-only service.'):
        UI.ok('Studio is running at http://127.0.0.1:8766')
        if not models_ready:
            UI.warn('The app is ready; generation needs approved Hugging Face access and model weights.')
        UI.line('  To stop it, send SIGTERM to the local asset_studio serve process.')
    else:
        UI.fail('The studio could not start. See ~/.local/share/trellislocalstudio/server.log')


def main():
    if not sys.stdin.isatty():
        print('Run ./install-asset-studio.command from Terminal to use the interactive installer.')
        return 2
    UI.banner()
    UI.line('  This assistant checks your Mac, installs the app and local generator,')
    UI.line('  guides Hugging Face sign-in, and can download the model files.')
    UI.line('  Setup is resumable. Existing environments and cached weights are reused.')
    UI.pause('Press Enter to begin.')

    if not system_check():
        UI.fail('Setup stopped before making changes.')
        return 1
    python = install_tools()
    if not python:
        UI.fail('Install the missing tools, then run this installer again.')
        return 1

    env = os.environ.copy()
    extra_paths = ['/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local/bin')]
    env['PATH'] = os.pathsep.join(extra_paths + [env.get('PATH', '')])
    env['TRELLIS_UPSTREAM_PYTHON'] = python
    env['TRELLIS_GENERATOR_PYTHON'] = str(GENERATOR_PYTHON)
    if not setup_app_environment(python, env):
        return 1
    if not setup_generator(python, env):
        return 1
    models_ready = huggingface_setup(env)
    launch(python, env, models_ready)
    UI.heading('Setup complete', 'The studio and its data remain on this Mac.')
    UI.line()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        UI.line('\n')
        UI.warn('Setup paused. Run ./install-asset-studio.command to continue.')
        sys.exit(130)
