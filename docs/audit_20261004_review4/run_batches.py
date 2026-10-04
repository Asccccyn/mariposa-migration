import os, sys, time, subprocess, signal, json
from pathlib import Path
import xml.etree.ElementTree as ET

BASE=Path(__file__).resolve().parent
ROOT=BASE/'snapshot'
OUT=BASE/'evidence'/os.environ.get('AUDIT_SUITE_OUT','suite')
OUT.mkdir(parents=True,exist_ok=True)
(ROOT/'.pytest_tmp'/'suite-temp').mkdir(parents=True,exist_ok=True)
PY='/Users/zhoujiaming/Projects/mariposa/.venv/bin/python'
env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','LANG','LC_ALL'}}
env.update(MARIPOSA_ALLOW_CREATE='1',MARIPOSA_SEMANTIC_PROVIDER='',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONHASHSEED='0')
files=sorted(ROOT.glob('tests/**/test_*.py'))
if len(sys.argv)>1:
    wanted=set(sys.argv[1:])
    files=[f for f in files if str(f.relative_to(ROOT)) in wanted]
rows=[]
global_start=time.monotonic()
for index,file in enumerate(files,1):
    rel=str(file.relative_to(ROOT))
    omit=['tests/acceptance/test_gate_cases.py::TestRET','tests/integration/test_dense_real_model_smoke.py::TestRealModelWarmupAndSmoke']
    for attempt in range(1,16):
        tag=f'{file.stem}-{attempt}'
        env['MARIPOSA_ROOT']=str(ROOT/'.pytest_tmp'/'suite-roots'/tag)
        args=[PY,'-m','pytest','-x','-q',rel,'--tb=short','--basetemp='+str(ROOT/'.pytest_tmp'/'suite-temp'/tag),'--junitxml='+str(OUT/(tag+'.xml'))]+['--deselect='+n for n in omit]
        started=time.monotonic();peak=0;reason=None
        with (OUT/(tag+'.log')).open('w') as log:
            proc=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            while proc.poll() is None:
                time.sleep(.4)
                try:
                    lines=subprocess.check_output(['/bin/ps','-axo','pid=,ppid=,rss='],text=True).splitlines()
                    procs=[tuple(map(int,l.split())) for l in lines if len(l.split())==3]
                    pids={proc.pid}
                    for _ in range(6):
                        pids.update(pid for pid,ppid,rss in procs if ppid in pids)
                    rss=sum(rss for pid,ppid,rss in procs if pid in pids)*1024
                    peak=max(peak,rss)
                except Exception as exc:
                    reason='monitor_failure:'+type(exc).__name__
                if time.monotonic()-started>180: reason='timeout_180s'
                if peak>1500*1024**2: reason='rss_limit_1500MiB'
                if time.monotonic()-global_start>1200: reason='total_limit_1200s'
                if reason:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                    break
        row={'file':rel,'attempt':attempt,'command':args,'exit_code':proc.returncode,'seconds':round(time.monotonic()-started,2),'peak_tree_rss_bytes':peak,'stop_reason':reason,'cases':[]}
        xp=OUT/(tag+'.xml')
        if xp.exists():
            for case in ET.parse(xp).iter('testcase'):
                classname=case.get('classname','');name=case.get('name','')
                module='.'.join(Path(rel).with_suffix('').parts)
                cls=classname[len(module):].strip('.')
                node=rel+('::'+cls.replace('.','::') if cls else '')+'::'+name
                status='failed' if case.find('failure') is not None else 'error' if case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
                row['cases'].append({'nodeid':node,'status':status})
        rows.append(row)
        (OUT/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        counts={s:sum(c['status']==s for c in row['cases']) for s in ['passed','failed','error','skipped']}
        print(f'{index}/{len(files)} {rel} attempt={attempt} rc={proc.returncode} {counts} {row["seconds"]}s peak={round(peak/1024**2)}MiB stop={reason}',flush=True)
        if proc.returncode in (0,5) or reason:break
        failed=[c['nodeid'] for c in row['cases'] if c['status']=='failed']
        if not failed:break
        omit.extend(c['nodeid'] for c in row['cases'])
    if time.monotonic()-global_start>1200:break
print('COMPLETE',flush=True)
