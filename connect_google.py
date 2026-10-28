"""Local OAuth, with loopback callback and state validation."""
import asyncio
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
import httpx
from google_calendar import secure_json

async def main():
    path=Path('data/google_client.json')
    if not path.exists(): raise SystemExit('Сначала сохраните OAuth JSON для Desktop app в data/google_client.json. См. README.md.')
    path.chmod(0o600)
    config=json.loads(path.read_text()).get('installed')
    if not config: raise SystemExit('Нужен OAuth-клиент типа Desktop app (installed).')
    state=secrets.token_urlsafe(32); result={}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            query=parse_qs(urlparse(self.path).query)
            ok=secrets.compare_digest(query.get('state',[''])[0],state)
            if ok: result.update(query)
            self.send_response(200 if ok else 400); self.end_headers()
            self.wfile.write(b'Google connection received. You can close this tab.' if ok else b'Invalid state.')
    server=HTTPServer(('127.0.0.1',0),Callback); server.timeout=1
    redirect=f'http://127.0.0.1:{server.server_port}/'
    url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(dict(client_id=config['client_id'],redirect_uri=redirect,response_type='code',scope='https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly',access_type='offline',prompt='consent',state=state))
    print('Открываю Google. Войдите и разрешите доступ к выбранному календарю. Ожидание до 5 минут.')
    webbrowser.open(url)
    deadline=time.time()+300
    try:
        while not result and time.time()<deadline: await asyncio.to_thread(server.handle_request)
    finally: server.server_close()
    if 'code' not in result: raise SystemExit('Подключение не завершено. Повторите команду после проверки доступа.')
    async with httpx.AsyncClient(timeout=30) as client:
        resp=await client.post('https://oauth2.googleapis.com/token',data=dict(client_id=config['client_id'],client_secret=config['client_secret'],code=result['code'][0],grant_type='authorization_code',redirect_uri=redirect))
        if resp.status_code!=200: raise SystemExit(f'Google не выдал доступ: HTTP {resp.status_code}')
        token=resp.json(); token['expires_at']=time.time()+token.get('expires_in',3600); secure_json('data/google_token.json',token)
    print('Авторизация сохранена локально. Выполните ./manage.sh check для проверки календаря.')
if __name__=='__main__': asyncio.run(main())
