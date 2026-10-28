"""Run as root on Beget with a verified source archive and its GitHub commit SHA.
Never ships .env or production data. Rollback restores code/config, NOT a stale database.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request

FILES=['bot.py','core.py','google_calendar.py','check.py','connect_google.py','web_api.py','web_auth.py','demo.py','requirements.txt','miniapp/index.html','miniapp/style.css','miniapp/app.js']
ROOT=Path('/opt/googlebot')
NGINX=Path('/etc/nginx/sites-available/lugovoy.shop')
DROPIN=Path('/etc/systemd/system/googlebot.service.d/miniapp.conf')

def run(*args):return subprocess.run(args,check=True,capture_output=True,text=True)
def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8087/meet/health',timeout=3) as r:
            return r.status==200 and json.load(r).get('ok') is True
    except Exception:return False

def deploy(archive,revision):
    if os.geteuid()!=0:raise RuntimeError('root required')
    if not __import__('re').fullmatch('[0-9a-f]{40}',revision):raise RuntimeError('Full GitHub SHA required')
    stage=Path('/opt/googlebot-releases')/revision
    stage.mkdir(parents=True,exist_ok=False)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path=Path(member.name)
            if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():raise RuntimeError('Unsafe archive')
        tar.extractall(stage)
    for name in FILES:
        if not (stage/name).is_file():raise RuntimeError('Incomplete release')
    expected=json.loads((stage/'release-manifest.json').read_text())
    for name,digest in expected.items():
        if hashlib.sha256((stage/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Release checksum mismatch')
    # Test the exact staged source with existing Linux dependencies, no real tokens/Google.
    for path in [stage,*stage.rglob('*')]:
        if path.is_file():path.chmod(0o644)
        elif path.is_dir():path.chmod(0o755)
    # discovery with cwd ensures staged imports, not currently live code
    result=subprocess.run(['runuser','-u','googlebot','--',str(ROOT/'.venv/bin/python'),'-B','-m','unittest','discover','-s','tests','-q'],cwd=stage,capture_output=True,text=True)
    if result.returncode:raise RuntimeError('Staged tests failed: '+result.stderr[-3000:])
    print(result.stderr.strip(),flush=True)
    before=NGINX.read_text()
    if 'location ^~ /meet/' in before:raise RuntimeError('Route already configured: inspect before updating')
    if '    location / {' not in before:raise RuntimeError('Unexpected nginx layout')
    block='''    location = /meet { return 301 /meet/; }
    location ^~ /meet/ {
        proxy_pass http://127.0.0.1:8087;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
        client_max_body_size 32k;
    }
'''
    backup=Path('/root/googlebot-backups')/time.strftime('%Y%m%d-%H%M%S',time.gmtime())
    backup.mkdir(parents=True,mode=0o700)
    (backup/'nginx.conf').write_text(before)
    if DROPIN.exists():shutil.copy2(DROPIN,backup/'miniapp.conf')
    (backup/'had-dropin').write_text(str(DROPIN.exists()))
    with tarfile.open(backup/'code.tar.gz','w:gz') as tar:
        for name in FILES:
            if (ROOT/name).exists():tar.add(ROOT/name,arcname=name)
    shutil.copy2(ROOT/'.env',backup/'env')
    run('systemctl','stop','googlebot')
    try:
        source=sqlite3.connect(str(ROOT/'data/booking.sqlite3'));dest=sqlite3.connect(str(backup/'booking.sqlite3'))
        source.backup(dest);dest.close();source.close()
        for name in FILES:
            target=ROOT/name;target.parent.mkdir(exist_ok=True)
            temp=target.with_suffix(target.suffix+'.incoming')
            shutil.copy2(stage/name,temp);temp.chmod(0o644);os.replace(temp,target)
        DROPIN.parent.mkdir(exist_ok=True)
        DROPIN.write_text('[Service]\nEnvironment=MINIAPP_URL=https://lugovoy.shop/meet/\nEnvironment=MINIAPP_PORT=8087\n')
        DROPIN.chmod(0o644)
        NGINX.write_text(before.replace('    location / {',block+'    location / {',1))
        run('nginx','-t');run('systemctl','daemon-reload');run('systemctl','start','googlebot')
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            if healthy():break
            time.sleep(1)
        else:raise RuntimeError('Mini App health check failed')
        run('systemctl','reload','nginx')
        (ROOT/'release.json').write_text(json.dumps({'revision':revision,'backup':str(backup),'deployed_at':time.time()},indent=2))
        print('DEPLOYED',revision,'BACKUP',backup,flush=True)
    except BaseException:
        NGINX.write_text(before)
        if (backup/'had-dropin').read_text()=='True':shutil.copy2(backup/'miniapp.conf',DROPIN)
        else:DROPIN.unlink(missing_ok=True)
        run('systemctl','stop','googlebot')
        # Keep the fresh database. The added table is backward compatible.
        for name in FILES:
            (ROOT/name).unlink(missing_ok=True)
        with tarfile.open(backup/'code.tar.gz') as tar:tar.extractall(ROOT)
        run('systemctl','daemon-reload');run('systemctl','start','googlebot')
        run('nginx','-t');run('systemctl','reload','nginx')
        print('ROLLED BACK code and config; current database preserved',flush=True)
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('archive');p.add_argument('revision');a=p.parse_args();deploy(a.archive,a.revision)
