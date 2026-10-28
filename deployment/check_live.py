"""Read-only live checks plus the explicitly requested Telegram menu configuration.
No test bookings, notifications or Google calls. Never print credentials/contact data.
"""
import hashlib,hmac,json,sqlite3,time
from pathlib import Path
from urllib.parse import urlencode
import httpx
from dotenv import dotenv_values
ROOT=Path('/opt/googlebot')
token=dotenv_values(ROOT/'.env')['TELEGRAM_BOT_TOKEN']
URL='https://lugovoy.shop/meet/'
with httpx.Client(timeout=12) as client:
    for url in ['https://lugovoy.shop/','https://www.lugovoy.shop/','https://currency.lugovoy.shop/','https://checking.lugovoy.shop/',URL,URL+'style.css',URL+'app.js',URL+'health']:
        r=client.get(url);print('HTTP',r.status_code,url,flush=True);assert r.status_code==200
    for path,expected in [('api/bootstrap',401),('demo/admin',404),('.env',404)]:
        r=client.get(URL+path);print('ACCESS',path,r.status_code,flush=True);assert r.status_code==expected
    db=sqlite3.connect('file:'+str(ROOT/'data/booking.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    uid=json.loads(db.execute('SELECT value FROM meta WHERE key=?',('admin_id',)).fetchone()[0])
    profile=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    user={'id':uid,'first_name':profile['name'] if profile else 'Owner','username':profile['username'] if profile else ''}
    values={'auth_date':str(int(time.time())),'user':json.dumps(user,separators=(',',':'),ensure_ascii=False)}
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(secret,'\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(),hashlib.sha256).hexdigest()
    headers={'X-Telegram-Init-Data':urlencode(values)}
    r=client.get(URL+'api/bootstrap',headers=headers);assert r.status_code==200
    data=r.json();assert data['admin'] and not data['demo'] and not data['calendar']
    print('AUTH admin=True demo=False google=False',flush=True)
    r=client.get(URL+'api/admin/settings',headers=headers);assert r.status_code==200;print('SETTINGS accessible',flush=True)
    r=client.get(URL+'api/slots?date=2026-11-10&duration=30',headers=headers);assert r.status_code==503;print('GOOGLE missing: slots blocked',flush=True)
    print('DB',db.execute('PRAGMA integrity_check').fetchone()[0],'requests',db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],flush=True)
    db.close()
    def bot(method,payload=None):
        for attempt in range(3):
            try:
                data={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in (payload or {}).items()}
                r=client.post('https://api.telegram.org/bot'+token+'/'+method,data=data)
                result=r.json()
                if result.get('ok'):return result['result']
                print('Telegram',method,'rejected',result.get('error_code'),flush=True)
            except Exception as exc:print('Telegram retry',method,type(exc).__name__,flush=True)
            time.sleep(1)
        raise RuntimeError('Telegram method not confirmed: '+method)
    me=bot('getMe');assert me['username']=='FinDir41bot';print('TELEGRAM @'+me['username'],flush=True)
    hook=bot('getWebhookInfo');assert not hook.get('url');print('WEBHOOK disabled, polling compatible',flush=True)
    old=bot('getChatMenuButton')
    backup=json.loads((ROOT/'release.json').read_text())['backup']
    if not Path(backup,'telegram-menu.json').exists():
        Path(backup,'telegram-menu.json').write_text(json.dumps(old))
    bot('setChatMenuButton',{'menu_button':{'type':'web_app','text':'Записаться','web_app':{'url':URL}}})
    menu=bot('getChatMenuButton');assert menu.get('type')=='web_app' and menu['web_app']['url']==URL
    print('MENU confirmed: Записаться -> '+URL,flush=True)
