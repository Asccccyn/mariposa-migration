"""Run a bounded, synthetic browser smoke test; never use the production root.

Requirements: existing frontend dependencies, Playwright (or
MARIPOSA_PLAYWRIGHT_MODULE), and Chromium (or MARIPOSA_CHROMIUM_EXECUTABLE).
The child server is loopback-only and always stopped. No models are loaded.
"""
from pathlib import Path
import json, os, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.request

ROOT=Path(__file__).resolve().parents[1]
PORT=int(os.environ.get('MARIPOSA_E2E_PORT','18881'))
BASE=f'http://127.0.0.1:{PORT}'

def stop(process):
    if process.poll() is None:
        os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()


def main():
    with socket.socket() as check:check.bind(('127.0.0.1',PORT))
    artifacts=Path(os.environ.get('MARIPOSA_E2E_ARTIFACTS', tempfile.mkdtemp(prefix='mariposa-browser-artifacts-')))
    artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mariposa-browser-') as tmp:
        tmp=Path(tmp);src=ROOT/'frontend';build=tmp/'frontend'
        shutil.copytree(src,build,ignore=shutil.ignore_patterns('node_modules','dist'))
        shutil.copytree(ROOT/'backend',tmp/'backend',ignore=shutil.ignore_patterns('__pycache__'))
        (build/'node_modules').mkdir()
        for p in (src/'node_modules').iterdir():
            if p.name not in {'.vite','.vite-temp'}:(build/'node_modules'/p.name).symlink_to(p.resolve(),target_is_directory=p.is_dir())
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(tmp/'backend')+os.pathsep+str(ROOT),'MARIPOSA_ROOT':str(tmp/'data'),'MARIPOSA_ALLOW_CREATE':'1','MARIPOSA_SEMANTIC_PROVIDER':'','MARIPOSA_RECALL_JUDGE_PROVIDER':'disabled','MARIPOSA_E2E_URL':BASE,'MARIPOSA_E2E_ARTIFACTS':str(artifacts),'MARIPOSA_E2E_FIXTURE':str(tmp/'data'/'fixture.json')}
        env.pop('MARIPOSA_MOOD_EDIT_OVERRIDE',None)
        subprocess.run(['node',str(src/'node_modules/typescript/bin/tsc'),'--noEmit'],cwd=src,env=env,check=True,timeout=60)
        subprocess.run(['node',str(src/'node_modules/vite/bin/vite.js'),'build','--outDir',str(build/'dist'),'--emptyOutDir'],cwd=build,env=env,check=True,timeout=60)
        subprocess.run([sys.executable,'-B',str(ROOT/'tests/e2e/seed.py')],cwd=ROOT,env=env,check=True,timeout=30)
        with (artifacts/'server.log').open('w') as log:
            server=subprocess.Popen([sys.executable,'-B','-m','uvicorn','mariposa.app:app','--host','127.0.0.1','--port',str(PORT)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                for _ in range(100):
                    if server.poll() is not None:raise RuntimeError('Isolated server failed; inspect '+str(artifacts/'server.log'))
                    try:
                        with urllib.request.urlopen(BASE+'/health',timeout=.5) as response:
                            if response.status==200:break
                    except OSError:time.sleep(.05)
                else:raise TimeoutError('Isolated server did not become ready')
                process=subprocess.Popen(['node',str(ROOT/'tests/e2e/library.smoke.cjs')],cwd=ROOT,env=env,start_new_session=True)
                try:code=process.wait(timeout=150)
                finally:stop(process)
                return code
            finally:stop(server)

if __name__=='__main__':raise SystemExit(main())
