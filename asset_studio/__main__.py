"""Launch a durable local server, browser, or automatically bootstrapped MCP transport."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from .core import DATA, ROOT


def ensure_server(port):
    url = 'http://127.0.0.1:'+str(port)
    def ready():
        try:
            with urllib.request.urlopen(url+'/api/status',timeout=1) as response:
                info = json.load(response)
            if info.get('service') != 'trellislocalstudio' or info.get('project_root') != str(ROOT) or info.get('data_dir') != str(DATA.resolve()):
                raise RuntimeError('This port belongs to another app or another studio. Use a different port.')
            return True
        except urllib.error.HTTPError as error:
            raise RuntimeError('This port is occupied. Use a different port.') from error
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            return False
    if ready(): return url
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'server.log').open('a') as log:
        process = subprocess.Popen([sys.executable,'-m','asset_studio','serve','--port',str(port)],cwd=ROOT,
            stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
    for _ in range(100):
        time.sleep(.15)
        if ready(): return url
        if process.poll() is not None:
            raise RuntimeError('Studio could not start. See '+str(DATA/'server.log'))
    raise RuntimeError('Studio is still starting. See '+str(DATA/'server.log'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['serve','open','ensure','mcp'],nargs='?',default='open')
    parser.add_argument('--port',type=int,default=int(os.environ.get('TRELLIS_STUDIO_PORT','8766')))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535: parser.error('Choose a port between 1024 and 65535.')
    if args.command == 'serve':
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(port=args.port),host='127.0.0.1',port=args.port,log_level='warning',access_log=False)
    else:
        url = ensure_server(args.port)
        if args.command == 'mcp':
            from .mcp_server import create_mcp
            create_mcp(url).run(transport='stdio')
        elif args.command == 'open':
            webbrowser.open(url)
            print('Trellis Local Studio: '+url+'\nGeneration continues after you close the browser.')
        else: print(url)


if __name__ == '__main__': main()
