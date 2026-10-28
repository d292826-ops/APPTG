"""Mini App transport. Receives the SAME Store/Scheduler/operation lock as Telegram."""
import asyncio
from collections import defaultdict, deque
from datetime import date, datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo
from aiohttp import web
from core import RuleError, LABELS, DURATIONS
from google_calendar import CalendarError
from web_auth import AuthError, validate_init_data

log = logging.getLogger('booking')
STATIC = Path(__file__).parent / 'miniapp'
TIME = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d\Z')
RANGES = {'daily_limit': (1,100), 'horizon_days': (1,365), 'approval_hours': (1,168),
          'min_hours': (0,8760), 'buffer_before': (0,1440), 'buffer_after': (0,1440)}
GROUPS = {'pending': ('pending','approving'), 'upcoming': ('approved','cancelling'),
          'archive': ('completed','cancelled','rejected','moved','conflict','expired')}

class APIError(Exception):
    def __init__(self, message, status=400, code='validation'):
        self.message, self.status, self.code = message, status, code

def text(value, name, limit, required=True):
    if not isinstance(value, str):
        raise APIError(f'Проверьте поле «{name}».')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise APIError(f'Поле «{name}»: от {1 if required else 0} до {limit} символов.')
    return value

def email(value):
    value = text(value, 'Почта', 254)
    if not re.fullmatch(r'[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+', value):
        raise APIError('Введите корректную электронную почту.')
    return value

def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise APIError(f'Введите целое число от {low} до {high}.')
    return value

def draft_data(body):
    result = {key: text(body.get(key,''), label, limit, required) for key,label,limit,required in
              [('title','Тема',150,True),('name','Имя',100,True),('description','Описание',1500,False)]}
    result['email'] = email(body.get('email'))
    result['duration'] = integer(body.get('duration'),15,90)
    if result['duration'] not in DURATIONS:
        raise APIError('Выберите длительность из списка.')
    result['start'] = integer(body.get('start'),1,2**40)
    if body.get('parent') is not None:
        result['parent'] = integer(body['parent'],1,2**53-1)
    return result

def checked_settings(body, current):
    if not isinstance(body, dict): raise APIError('Проверьте настройки.')
    result = dict(current)
    allowed = set(RANGES) | {'timezone','start','end','weekdays'}
    if set(body) - allowed: raise APIError('Неизвестная настройка.')
    for key,value in body.items():
        if key in RANGES: result[key] = integer(value,*RANGES[key])
        elif key == 'timezone':
            if not isinstance(value,str): raise APIError('Выберите часовой пояс.')
            try: ZoneInfo(value)
            except (ValueError,KeyError): raise APIError('Неизвестный часовой пояс.') from None
            result[key] = value
        elif key in ('start','end'):
            if not isinstance(value,str) or not TIME.fullmatch(value): raise APIError('Время в формате ЧЧ:ММ.')
            result[key] = value
        elif key == 'weekdays':
            if not isinstance(value,list) or not value: raise APIError('Выберите рабочие дни.')
            result[key] = sorted(set(integer(x,0,6) for x in value))
    if result['start'] >= result['end']: raise APIError('Конец дня должен быть позже начала.')
    return result

def exception_data(body):
    raw = body.get('date')
    try:
        if not isinstance(raw,str) or date.fromisoformat(raw).isoformat()!=raw: raise ValueError()
    except ValueError: raise APIError('Выберите дату.') from None
    out = {'date': raw}
    if body.get('start') or body.get('end'):
        a,b = body.get('start'),body.get('end')
        if not isinstance(a,str) or not isinstance(b,str) or not TIME.fullmatch(a) or not TIME.fullmatch(b) or a>=b:
            raise APIError('Задайте начало и конец интервала.')
        out.update(start=a,end=b)
    return out

def create_app(store, scheduler, operation_lock, token='', demo=False):
    # demo is passed only by the loopback demo entry point, NEVER from a request/env flag.
    buckets = defaultdict(deque)
    store.db.execute('CREATE TABLE IF NOT EXISTS web_operations(user_id INTEGER, key TEXT, fingerprint TEXT, request_id INTEGER, created REAL, PRIMARY KEY(user_id,key))')
    store.db.commit()

    @web.middleware
    async def middleware(request, handler):
        try:
            if request.path.startswith('/meet/api/'):
                origin = request.headers.get('Origin')
                if origin and origin != f'{request.headers.get("X-Forwarded-Proto",request.scheme)}://{request.host}':
                    raise APIError('Источник запроса не разрешён.',403,'forbidden')
                if demo:
                    role = request.cookies.get('demo_role','user')
                    user = {'id': 1001 if role=='admin' else 1002, 'first_name':'Демо', 'last_name':'Администратор' if role=='admin' else 'Пользователь','username':'demo'}
                else:
                    user = validate_init_data(request.headers.get('X-Telegram-Init-Data',''),token)
                request['user'] = user
                uid = user['id']; now=time.monotonic()
                if len(buckets)>2000:
                    for key in list(buckets):
                        if not buckets[key] or now-buckets[key][-1]>60: buckets.pop(key,None)
                    if len(buckets)>2000: raise APIError('Сервис занят. Повторите позже.',429,'rate_limit')
                queue=buckets[uid]
                while queue and now-queue[0]>60: queue.popleft()
                if len(queue)>=120: raise APIError('Слишком много запросов. Подождите минуту.',429,'rate_limit')
                queue.append(now)
                # This also protects synchronous settings/profile changes from calendar awaits in bot.
                async def locked_handler():
                    async with operation_lock:
                        return await handler(request)
                return await asyncio.wait_for(locked_handler(),timeout=20)
            return await handler(request)
        except AuthError as exc:
            return web.json_response({'error':str(exc),'code':'auth'},status=401)
        except APIError as exc:
            return web.json_response({'error':exc.message,'code':exc.code},status=exc.status)
        except CalendarError:
            return web.json_response({'error':'Календарь недоступен. Попробуйте позже.','code':'calendar'},status=503)
        except RuleError as exc:
            return web.json_response({'error':str(exc),'code':'conflict'},status=409)
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            return web.json_response({'error':'Проверьте данные запроса.','code':'validation'},status=400)
        except asyncio.TimeoutError:
            return web.json_response({'error':'Проверка занимает больше времени. Проверьте статус заявки перед повтором.','code':'timeout'},status=504)
        except web.HTTPException as exc:
            if request.path.startswith('/meet/api/'):
                return web.json_response({'error':'Запрос не поддерживается.','code':'http'},status=exc.status)
            raise
        except Exception as exc:
            log.error('miniapp_failed kind=%s',type(exc).__name__)
            return web.json_response({'error':'Не удалось выполнить действие. Повторите позже.','code':'server'},status=500)

    app=web.Application(middlewares=[middleware],client_max_size=32768)
    def admin(request):
        if request['user']['id'] != store.get('admin_id'):
            raise APIError('Доступ только администратору.',403,'forbidden')
    def calendar_required():
        if not scheduler.cal.configured():
            raise APIError('Запись пока недоступна: календарь не подключён.',503,'calendar')
    def row_for(request):
        number=int(request.match_info['id'])
        record=store.db.execute('SELECT * FROM requests WHERE id=?',(number,)).fetchone()
        if not record or (record['user_id']!=request['user']['id'] and request['user']['id']!=store.get('admin_id')):
            raise APIError('Заявка недоступна.',404,'not_found')
        return dict(record)
    def profile(request):
        u=request['user']; uid=u['id']; old=store.user(uid)
        store.profile(uid,name=old['name'] or ' '.join(filter(None,[u.get('first_name'),u.get('last_name')])),username=u.get('username',''))
        return store.user(uid)
    def public_row(row):
        out=dict(row); out['status_label']=LABELS.get(out['status'],out['status'])
        return out
    async def body(request):
        result=await request.json()
        if not isinstance(result,dict): raise APIError('Ожидается объект данных.')
        return result

    async def bootstrap(request):
        store.cleanup(); u=profile(request); settings=store.settings()
        is_admin=u['id']==store.get('admin_id')
        pending=store.db.execute("SELECT COUNT(*) FROM requests WHERE status IN ('pending','approving')").fetchone()[0] if is_admin else 0
        return web.json_response({'profile':u,'admin':is_admin,'settings':settings if is_admin else {k:settings[k] for k in ('timezone','weekdays','start','end','horizon_days')},
             'calendar':scheduler.cal.configured(),'pending':pending,'demo':demo,'today':datetime.now(ZoneInfo(settings['timezone'])).date().isoformat()})
    async def save_profile(request):
        b=await body(request); u=profile(request)
        store.profile(u['id'],name=text(b.get('name'),'Имя',100),email=email(b.get('email')))
        return web.json_response({'ok':True})
    async def slots(request):
        calendar_required(); b=request.query
        duration=int(b['duration']); day=date.fromisoformat(b['date'])
        parent=int(b['parent']) if b.get('parent') else None
        if parent:
            r=store.owned(parent,request['user']['id'])
            if r['user_id']!=request['user']['id'] or r['status']!='approved' or r['start']<=time.time(): raise APIError('Перенос недоступен.')
        result=await scheduler.available(day,duration,parent=parent)
        return web.json_response({'slots':result})
    async def requests(request):
        store.cleanup(); uid=request['user']['id']
        all_users=request.query.get('scope')=='admin'
        if all_users: admin(request)
        group=request.query.get('group','pending')
        if group not in GROUPS: raise APIError('Неизвестный раздел.')
        offset=max(0,min(int(request.query.get('offset','0')),100000))
        statuses=GROUPS[group]; placeholders=','.join('?' for _ in statuses)
        where=f'status IN ({placeholders})'; args=list(statuses)
        if not all_users: where+=' AND user_id=?'; args.append(uid)
        order={'pending':'deadline ASC,id ASC','upcoming':'start ASC,id ASC','archive':'closed DESC,id DESC'}[group]
        records=store.db.execute(f'SELECT * FROM requests WHERE {where} ORDER BY {order} LIMIT 21 OFFSET ?',(*args,offset)).fetchall()
        return web.json_response({'items':[public_row(r) for r in records[:20]],'more':len(records)>20})
    async def submit(request):
        b=await body(request); d=draft_data(b); u=profile(request)
        key=request.headers.get('Idempotency-Key','')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}',key): raise APIError('Обновите форму и повторите отправку.')
        digest=hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        previous=store.db.execute('SELECT * FROM web_operations WHERE user_id=? AND key=?',(u['id'],key)).fetchone()
        if previous:
            if previous['fingerprint']!=digest: raise APIError('Эта отправка уже использована. Начните новую заявку.',409,'conflict')
            return web.json_response({'request':public_row(store.owned(previous['request_id'],u['id']))})
        calendar_required()
        if d.get('parent'):
            parent=store.owned(d['parent'],u['id'])
            if parent['user_id']!=u['id'] or parent['start']<=time.time(): raise APIError('Перенос недоступен.')
        r=await scheduler.submit(u['id'],d,operation=(key,digest))
        store.db.execute('DELETE FROM web_operations WHERE created<?',(time.time()-31*86400,))
        store.db.commit()
        return web.json_response({'request':public_row(r)},status=201)
    async def action(request):
        r=row_for(request); b=await body(request); uid=request['user']['id']; operation=b.get('action')
        if operation=='approve':
            admin(request); calendar_required(); result=await scheduler.approve(r['id'],uid)
        elif operation=='reject':
            admin(request); await scheduler.reject(r['id'],uid,text(b.get('comment',''),'Комментарий',500,False)); result=store.row(r['id'])
        elif operation=='cancel':
            if r['status'] in ('approved','cancelling'): calendar_required()
            await scheduler.cancel(r['id'],uid); result=store.row(r['id'])
        elif operation=='ban':
            admin(request)
            if r['user_id']==store.get('admin_id'): raise APIError('Нельзя заблокировать администратора.')
            await scheduler.ban(r['user_id'],uid); result=store.row(r['id'])
        else: raise APIError('Неизвестное действие.')
        return web.json_response({'request':public_row(result)})
    async def settings(request):
        admin(request)
        if request.method=='POST': store.set('settings',checked_settings(await body(request),store.settings()))
        return web.json_response({'settings':store.settings()})
    async def exceptions(request):
        admin(request); item=exception_data(await body(request)); v=store.settings(); items=list(v['closed'])
        if request.method=='POST':
            if len(items)>=366: raise APIError('Слишком много исключений. Удалите старые.')
            if item not in items: items.append(item)
        else: items=[x for x in items if x!=item]
        v['closed']=items; store.set('settings',v)
        return web.json_response({'settings':v})
    async def blacklist(request):
        admin(request)
        if request.method=='POST':
            b=await body(request); await scheduler.ban(integer(b.get('user_id'),1,2**53-1),request['user']['id'],False)
        return web.json_response({'items':[dict(r) for r in store.db.execute('SELECT id,name,username FROM users WHERE banned=1 ORDER BY name')]})
    async def calendar_status(request):
        admin(request)
        if not scheduler.cal.configured(): return web.json_response({'state':'missing','label':'Не подключён','checked_at':None})
        try:
            await scheduler.cal.busy(time.time(),time.time()+3600)
            return web.json_response({'state':'ok','label':'Доступ проверен','checked_at':time.time()})
        except CalendarError as exc:
            # A temporary outage is different from authorization failure. Do not expose credentials/errors.
            message=str(exc).lower()
            auth=any(x in message for x in ('авториза','подключ','401','invalid_grant'))
            return web.json_response({'state':'auth' if auth else 'error','label':'Нужна повторная авторизация' if auth else 'Не удалось проверить — временная ошибка','checked_at':time.time()})
    async def health(request): return web.json_response({'ok':True})
    async def static(request):
        name=request.match_info.get('name','index.html')
        if name not in ('index.html','app.js','style.css'): raise web.HTTPNotFound()
        return web.FileResponse(STATIC/name)
    async def demo_role(request):
        if not demo: raise web.HTTPNotFound()
        response=web.HTTPFound('/meet/')
        response.set_cookie('demo_role',request.match_info['role'],httponly=True,samesite='Strict',path='/meet/')
        return response
    async def security_headers(request,response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; object-src 'none'"
    app.on_response_prepare.append(security_headers)
    app.router.add_get('/meet/api/bootstrap',bootstrap)
    app.router.add_post('/meet/api/profile',save_profile)
    app.router.add_get('/meet/api/slots',slots)
    app.router.add_get('/meet/api/requests',requests)
    app.router.add_post('/meet/api/requests',submit)
    app.router.add_post('/meet/api/requests/{id:\\d+}/action',action)
    app.router.add_get('/meet/api/admin/settings',settings)
    app.router.add_post('/meet/api/admin/settings',settings)
    app.router.add_post('/meet/api/admin/exceptions',exceptions)
    app.router.add_delete('/meet/api/admin/exceptions',exceptions)
    app.router.add_get('/meet/api/admin/blacklist',blacklist)
    app.router.add_post('/meet/api/admin/blacklist',blacklist)
    app.router.add_post('/meet/api/admin/calendar',calendar_status)
    app.router.add_get('/meet/health',health)
    app.router.add_get('/meet/demo/{role:user|admin}',demo_role)
    app.router.add_get('/meet/',static)
    app.router.add_get('/meet/{name}',static)
    return app

async def start_web(store,scheduler,lock,token,port=8087):
    runner=web.AppRunner(create_app(store,scheduler,lock,token),access_log=None)
    await runner.setup()
    try: await web.TCPSite(runner,'127.0.0.1',port).start()
    except BaseException:
        await runner.cleanup(); raise
    return runner
