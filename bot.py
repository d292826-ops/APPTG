import asyncio
import fcntl
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from dotenv import load_dotenv
from core import Store, Scheduler, RuleError, LABELS, DURATIONS
from google_calendar import GoogleCalendar, CalendarError

log=logging.getLogger('booking')

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=c) for t,c in row] for row in rows])

def valid_email(s): return bool(re.fullmatch(r'[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+',s)) and len(s)<=254

async def main():
    load_dotenv(); token=os.getenv('TELEGRAM_BOT_TOKEN')
    if not token: raise SystemExit('Не задан TELEGRAM_BOT_TOKEN в локальном .env')
    Path('data').mkdir(exist_ok=True); os.chmod('data',0o700)
    singleton=open('data/process.lock','w')
    try: fcntl.flock(singleton,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Бот уже запущен.')
    Path('data/bot.pid').write_text(str(os.getpid()))
    s=Store(); cal=GoogleCalendar(os.getenv('GOOGLE_CALENDAR_ID','primary')); service=Scheduler(s,cal)
    if not s.get('admin_id') and not Path('data/admin_claim.txt').exists():
        Path('data/admin_claim.txt').write_text(secrets.token_hex(16)); Path('data/admin_claim.txt').chmod(0o600)
    dp=Dispatcher(); operation_lock=asyncio.Lock()
    from aiogram.client.session.aiohttp import AiohttpSession
    session = AiohttpSession(timeout=20)
    if os.getenv('TELEGRAM_IPV4_ONLY') == '1':
        import socket
        session._connector_init['family'] = socket.AF_INET
    async with Bot(token, session=session) as bot:
        async def home(target,uid):
            rows=[[('Записаться на встречу','book')],[('Мои заявки','mine:0')],[('Мои данные','profile')]]
            if s.get('admin_id')==uid: rows.append([('Администрирование','admin')])
            markup=kb(rows)
            if os.getenv('MINIAPP_URL'):
                markup.inline_keyboard.insert(0,[InlineKeyboardButton(text='Открыть приложение',web_app=WebAppInfo(url=os.environ['MINIAPP_URL']))])
            await target.answer('Выберите действие. Встречи назначаются после согласования.\nЧасовой пояс: '+s.settings()['timezone'],reply_markup=markup)
        async def fail(target,exc):
            if isinstance(exc,RuleError): await target.answer(str(exc)); return
            log.error('operation_failed kind=%s',type(exc).__name__)
            if isinstance(exc,CalendarError): text=str(exc)
            else: text='Техническая ошибка. Действие не подтверждено; попробуйте позже.'
            await target.answer(text)
            admin=s.get('admin_id')
            if admin:
                # Throttle repeated integration alerts, without leaking request contents.
                last=s.get('last_error_alert',0)
                if time.time()-last>300:
                    s.notice(admin,'Технический сбой: '+text); s.set('last_error_alert',time.time())
        async def request_card(target,n,uid):
            r=s.owned(n,uid); rows=[]
            if r['status'] in ('pending','approved','cancelling'): rows.append([('Отменить','confirmcancel:'+str(n))])
            if r['status']=='approved' and r['start']>time.time(): rows.append([('Перенести','move:'+str(n))])
            if uid==s.get('admin_id'):
                if r['status'] in ('pending','approving'): rows.append([('Согласовать','approve:'+str(n))])
                if r['status']=='pending': rows.append([('Отклонить','reject:'+str(n))])
                rows.append([('Заблокировать пользователя','confirmban:'+str(r['user_id']))])
            rows.append([('В меню','home')]); await target.answer(service.card(r),reply_markup=kb(rows))
        async def listing(target,uid,admin=False,offset=0):
            if admin: s.admin(uid)
            s.cleanup()
            query='SELECT * FROM requests '+('' if admin else 'WHERE user_id=? ')+'ORDER BY created DESC LIMIT 9 OFFSET ?'
            records=s.db.execute(query,(offset,) if admin else (uid,offset)).fetchall()
            rows=[]
            for x in records[:8]:
                title=x['title'][:24]
                rows.append([(f"№{x['id']} {service.stamp(x['start'])} · {title} · {LABELS[x['status']]}",f"view:{x['id']}")])
            prefix='all' if admin else 'mine'; nav=[]
            if offset: nav.append(('←',f'{prefix}:{max(0,offset-8)}'))
            if len(records)>8: nav.append(('→',f'{prefix}:{offset+8}'))
            if nav: rows.append(nav)
            rows.append([('В меню','home')]); await target.answer('Заявки' if records else 'Заявок пока нет.',reply_markup=kb(rows))
        async def render(target,uid,step):
            d=s.session(uid); d['step']=step; s.session(uid,d)
            back=[[('Назад','back'),('В меню','home')]]
            if step=='duration':
                await target.answer('Выберите длительность встречи:',reply_markup=kb([[(str(x)+' мин',f'duration:{x}')] for x in DURATIONS]+[[('В меню','home')]]))
            elif step=='date':
                tz=ZoneInfo(s.settings()['timezone']); today=datetime.now(tz).date()
                monday=today-timedelta(days=today.weekday())+timedelta(weeks=d.get('week',0))
                rows=[]; names=['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
                for i in range(7):
                    date=monday+timedelta(days=i)
                    if today<=date<=today+timedelta(days=s.settings()['horizon_days']): rows.append([(f'{names[i]} {date:%d.%m}',f'date:{date.isoformat()}')])
                nav=[]
                if d.get('week',0)>0: nav.append(('← Неделя',f"week:{d['week']-1}"))
                if monday+timedelta(days=7)<=today+timedelta(days=s.settings()['horizon_days']): nav.append(('Неделя →',f"week:{d.get('week',0)+1}"))
                if nav: rows.append(nav)
                await target.answer(f'Выберите дату. Неделя {monday:%d.%m}–{monday+timedelta(days=6):%d.%m}.\nПояс: {tz.key}',reply_markup=kb(rows+back))
            elif step=='time':
                slots=await service.available(d['date'],d['duration'],parent=d.get('parent'))
                rows=[]
                for i in range(0,len(slots),3): rows.append([(datetime.fromtimestamp(t,ZoneInfo(s.settings()['timezone'])).strftime('%H:%M'),f'time:{int(t)}') for t in slots[i:i+3]])
                await target.answer(('Выберите время начала:' if slots else 'Нет подходящего свободного времени. Выберите другую дату.')+'\nПояс: '+s.settings()['timezone'],reply_markup=kb(rows+back))
            elif step in ('title','email','name','description'):
                prompts=dict(title='Введите название встречи (до 150 символов).',email='Введите электронную почту для приглашения.',name='Введите имя и фамилию (до 100 символов).',description='Введите описание (до 1500 символов) или нажмите «Пропустить».')
                rows=[]
                if d.get(step): rows.append([('Оставить: '+d[step][:35],'keep:'+step)])
                if step=='description': rows.append([('Пропустить','skip')])
                await target.answer(prompts[step],reply_markup=kb(rows+back))
            elif step=='review':
                text=(f"Проверьте заявку\n{d['title']}\n{service.stamp(d['start'])} — {service.stamp(d['start']+d['duration']*60)}\n"
                      f"Пояс: {s.settings()['timezone']}\n{d['name']}\n{d['email']}\n{d.get('description','')}")
                await target.answer(text,reply_markup=kb([[('Отправить заявку','submit')]]+back))
        async def begin(target,uid,parent=None):
            u=s.user(uid)
            if u['banned']: raise RuleError('Создание новых заявок недоступно.')
            d=dict(name=u['name'],email=u['email'],week=0)
            if parent:
                r=s.owned(parent,uid)
                if r['status']!='approved' or r['start']<=time.time(): raise RuleError('Перенос недоступен.')
                d.update(parent=parent,title=r['title'],description=r['description'],name=r['name'],email=r['email'])
            s.session(uid,d); await render(target,uid,'duration')
        async def admin_menu(target,uid):
            s.admin(uid)
            status='подключён' if cal.configured() else 'не подключён'
            await target.answer('Администрирование\nGoogle Календарь: '+status,reply_markup=kb([[('Заявки','all:0')],[('Настройки расписания','settings')],[('Чёрный список','blacklist')],[('Проверить календарь','checkgoogle')],[('В меню','home')]]))
        async def settings_menu(target,uid):
            s.admin(uid); v=s.settings(); labels=dict(timezone='Часовой пояс',weekdays='Рабочие дни',start='Начало дня',end='Конец дня',min_hours='Минимум часов до встречи',horizon_days='Горизонт в днях',buffer_before='Буфер до, мин',buffer_after='Буфер после, мин',daily_limit='Встреч в день',approval_hours='Срок согласования, ч')
            rows=[[(f'{name}: {v[key]}','setting:'+key)] for key,name in labels.items()]
            rows.extend([[('Закрыть дату или часы','closeinterval')],[('Список исключений / удалить','exceptions')],[('Назад','admin')]])
            await target.answer('Настройки. Изменения действуют для новой записи. Уже созданные встречи автоматически не отменяются.',reply_markup=kb(rows))
        @dp.message(Command('start','help'))
        async def start(m:Message):
            if m.chat.type!='private': return
            s.session(m.from_user.id,{})
            u=s.user(m.from_user.id)
            s.profile(m.from_user.id,name=u['name'] or m.from_user.full_name,username=m.from_user.username or '')
            await home(m,m.from_user.id)
        @dp.message(Command('id'))
        async def identify(m:Message):
            if m.chat.type=='private': await m.answer(f'Ваш Telegram ID: {m.from_user.id}')
        @dp.message(Command('claim'))
        async def claim(m:Message):
            if m.chat.type!='private': return
            path=Path('data/admin_claim.txt'); parts=(m.text or '').split()
            if s.get('admin_id') or not path.exists(): await m.answer('Назначение администратора уже завершено.'); return
            if len(parts)!=2 or not secrets.compare_digest(parts[1],path.read_text().strip()): await m.answer('Неверный одноразовый код.'); return
            s.set('admin_id',m.from_user.id); path.unlink()
            try: await m.delete()
            except Exception: pass
            await m.answer('Вы назначены администратором этого бота.'); await home(m,m.from_user.id)
        @dp.message(Command('admin'))
        async def admin_cmd(m:Message):
            if m.chat.type!='private': return
            try: await admin_menu(m,m.from_user.id)
            except Exception as exc: await fail(m,exc)
        @dp.callback_query()
        async def callback(q:CallbackQuery):
            if not q.message or q.message.chat.type!='private': await q.answer(); return
            await q.answer(); uid=q.from_user.id; target=q.message; cmd,_,arg=(q.data or '').partition(':')
            async with operation_lock:
                try:
                    if cmd=='home': s.session(uid,{}); await home(target,uid)
                    elif cmd=='book': await begin(target,uid)
                    elif cmd=='move': await begin(target,uid,int(arg))
                    elif cmd in ('mine','all'): await listing(target,uid,cmd=='all',max(0,int(arg)))
                    elif cmd=='view': await request_card(target,int(arg),uid)
                    elif cmd=='profile':
                        u=s.user(uid); s.session(uid,dict(step='profile_name'))
                        await target.answer(f"Ваши данные\nИмя: {u['name']}\nПочта: {u['email'] or 'не задана'}\nВведите новое имя или /start для выхода.")
                    elif cmd=='admin': await admin_menu(target,uid)
                    elif cmd=='settings': await settings_menu(target,uid)
                    elif cmd=='setting':
                        s.admin(uid)
                        if arg not in s.settings() or arg=='closed': raise RuleError('Неизвестная настройка.')
                        s.session(uid,dict(step='setting',key=arg))
                        helptext='Введите значение. Время: ЧЧ:ММ; часовой пояс: например Asia/Kamchatka; рабочие дни: номера 1–7 через запятую (Пн–Вс); остальные параметры: целое число. /admin — выход.'
                        await target.answer(helptext)
                    elif cmd=='closeinterval':
                        s.admin(uid); s.session(uid,dict(step='closeinterval'))
                        await target.answer('Введите дату ГГГГ-ММ-ДД для закрытия всего дня или ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ для закрытия часов. Уже назначенные встречи не отменяются.')
                    elif cmd=='exceptions':
                        s.admin(uid); rows=[]
                        for i,x in enumerate(s.settings()['closed']): rows.append([(x['date']+' '+x.get('start','весь день')+' — удалить',f'openinterval:{i}')])
                        await target.answer('Исключения. Нажатие удаляет ограничение.',reply_markup=kb(rows+[[('Назад','settings')]]))
                    elif cmd=='openinterval':
                        s.admin(uid); settings=s.settings(); i=int(arg)
                        if 0<=i<len(settings['closed']): settings['closed'].pop(i); s.set('settings',settings)
                        await settings_menu(target,uid)
                    elif cmd=='blacklist':
                        s.admin(uid); rows=[[(str(x['id'])+' '+(x['name'] or '')[:25],f'unban:{x["id"]}')] for x in s.db.execute('SELECT * FROM users WHERE banned=1').fetchall()]
                        await target.answer('Заблокированные пользователи. Нажмите для разблокировки.',reply_markup=kb(rows+[[('Назад','admin')]]))
                    elif cmd=='confirmban':
                        s.admin(uid); await target.answer('Заблокировать пользователя и отклонить его ожидающие заявки? Согласованные встречи сохранятся.',reply_markup=kb([[('Да, заблокировать','ban:'+arg)],[('В меню','home')]]))
                    elif cmd in ('ban','unban'):
                        await service.ban(int(arg),uid,cmd=='ban'); await target.answer('Настройка блокировки сохранена.')
                    elif cmd=='checkgoogle':
                        s.admin(uid); await cal.busy(time.time(),time.time()+86400); await target.answer('Доступ к Google Календарю проверен.')
                    elif cmd=='approve':
                        r=await service.approve(int(arg),uid); await request_card(target,r['id'],uid)
                    elif cmd=='reject':
                        s.admin(uid); r=s.row(int(arg))
                        if r['status']!='pending': raise RuleError('Заявка уже обработана.')
                        s.session(uid,dict(step='reject',request_id=r['id']))
                        await target.answer('Введите причину отказа или «-», чтобы отклонить без комментария.')
                    elif cmd=='confirmcancel':
                        s.owned(int(arg),uid); await target.answer('Подтвердите отмену заявки или встречи.',reply_markup=kb([[('Да, отменить','cancel:'+arg)],[('Вернуться','view:'+arg)]]))
                    elif cmd=='cancel': await service.cancel(int(arg),uid); await target.answer('Отмена выполнена.')
                    else:
                        d=s.session(uid); step=d.get('step')
                        if not step: raise RuleError('Эта форма устарела. Начните запись заново.')
                        if cmd=='duration' and step=='duration':
                            if int(arg) not in DURATIONS: raise RuleError('Недопустимая длительность.')
                            d['duration']=int(arg); d.pop('start',None); s.session(uid,d); await render(target,uid,'date')
                        elif cmd=='week' and step=='date':
                            week=int(arg)
                            if not 0<=week<=54: raise RuleError('Недопустимая неделя.')
                            d['week']=week; s.session(uid,d); await render(target,uid,'date')
                        elif cmd=='date' and step=='date':
                            datetime.fromisoformat(arg); d['date']=arg; d.pop('start',None); s.session(uid,d); await render(target,uid,'time')
                        elif cmd=='time' and step=='time':
                            slots=await service.available(d['date'],d['duration'],parent=d.get('parent'))
                            if int(arg) not in slots: raise RuleError('Это время больше недоступно.')
                            d['start']=int(arg); s.session(uid,d); await render(target,uid,'title')
                        elif cmd=='back':
                            order=['duration','date','time','title','email','name','description','review']
                            if step not in order: raise RuleError('Начните запись из меню.')
                            await render(target,uid,order[max(0,order.index(step)-1)])
                        elif cmd=='keep' and arg==step and step in ('title','email','name','description'):
                            if not d.get(step): raise RuleError('Нет сохранённого значения.')
                            if step=='email' and not valid_email(d['email']): raise RuleError('Введите корректную почту.')
                            await render(target,uid,dict(title='email',email='name',name='description',description='review')[step])
                        elif cmd=='skip' and step=='description': d['description']=''; s.session(uid,d); await render(target,uid,'review')
                        elif cmd=='submit' and step=='review':
                            r=await service.submit(uid,d); s.session(uid,{})
                            await target.answer(f"Заявка №{r['id']} отправлена на согласование. Резерв до {service.stamp(r['deadline'])}.")
                            await request_card(target,r['id'],uid)
                        else: raise RuleError('Кнопка относится к другому шагу. Используйте последнее сообщение или /start.')
                except Exception as exc: await fail(target,exc)
        @dp.message(F.text)
        async def text(m:Message):
            if m.chat.type!='private': return
            uid=m.from_user.id; value=m.text.strip(); d=s.session(uid); step=d.get('step')
            async with operation_lock:
                try:
                    if step in ('title','email','name','description'):
                        limits=dict(title=150,email=254,name=100,description=1500)
                        if not value or len(value)>limits[step]: raise RuleError(f'Введите текст длиной от 1 до {limits[step]} символов.')
                        if step=='email' and not valid_email(value): raise RuleError('Введите корректную электронную почту.')
                        d[step]=value; s.session(uid,d)
                        await render(m,uid,dict(title='email',email='name',name='description',description='review')[step])
                    elif step=='profile_name':
                        if not 1<=len(value)<=100: raise RuleError('Имя должно содержать от 1 до 100 символов.')
                        s.profile(uid,name=value); s.session(uid,dict(step='profile_email')); await m.answer('Введите электронную почту.')
                    elif step=='profile_email':
                        if not valid_email(value): raise RuleError('Введите корректную почту.')
                        s.profile(uid,email=value); s.session(uid,{}); await m.answer('Данные сохранены.'); await home(m,uid)
                    elif step=='reject':
                        if len(value)>500: raise RuleError('Комментарий до 500 символов.')
                        await service.reject(d['request_id'],uid,'' if value=='-' else value); s.session(uid,{}); await m.answer('Заявка отклонена.')
                    elif step=='setting':
                        s.admin(uid); settings=s.settings(); key=d['key']
                        if key=='timezone': ZoneInfo(value); parsed=value
                        elif key in ('start','end'):
                            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value): raise RuleError('Время в формате ЧЧ:ММ.')
                            parsed=value
                        elif key=='weekdays':
                            parsed=sorted(set(int(x.strip())-1 for x in value.split(',')))
                            if not parsed or not all(0<=x<=6 for x in parsed): raise RuleError('Дни от 1 до 7.')
                        else:
                            parsed=int(value); low=1 if key in ('daily_limit','horizon_days','approval_hours') else 0
                            high={'daily_limit':100,'horizon_days':365,'approval_hours':168,'min_hours':8760,'buffer_before':1440,'buffer_after':1440}[key]
                            if not low<=parsed<=high: raise RuleError(f'Допустимо от {low} до {high}.')
                        settings[key]=parsed
                        if settings['start']>=settings['end']: raise RuleError('Конец рабочего дня должен быть позже начала.')
                        s.set('settings',settings); s.session(uid,{}); await settings_menu(m,uid)
                    elif step=='closeinterval':
                        s.admin(uid); parts=value.split()
                        if len(parts) not in (1,3): raise RuleError('Формат: ГГГГ-ММ-ДД или ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ.')
                        date=datetime.strptime(parts[0],'%Y-%m-%d').date(); item={'date':str(date)}
                        if len(parts)==3:
                            for x in parts[1:]:
                                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',x): raise RuleError('Время в формате ЧЧ:ММ.')
                            if parts[1]>=parts[2]: raise RuleError('Конец должен быть позже начала.')
                            item.update(start=parts[1],end=parts[2])
                        settings=s.settings()
                        if item not in settings['closed']: settings['closed'].append(item)
                        s.set('settings',settings); s.session(uid,{}); await m.answer('Ограничение добавлено. Уже созданные встречи сохранены.'); await settings_menu(m,uid)
                    else: await home(m,uid)
                except (ValueError,KeyError) as exc: await m.answer('Не удалось распознать значение. Проверьте формат и повторите ввод.')
                except Exception as exc: await fail(m,exc)
        @dp.message()
        async def other(m:Message):
            if m.chat.type=='private': await m.answer('Используйте кнопки меню или отправьте /start.')
        @dp.errors()
        async def error(event):
            log.error('telegram_update_failed kind=%s',type(event.exception).__name__); return True
        async def maintenance():
            while True:
                try:
                    async with operation_lock:
                        s.cleanup()
                        # Retry journalled operations after a network failure or process restart.
                        admin=s.get('admin_id')
                        if admin:
                            for r in s.db.execute("SELECT id,status FROM requests WHERE status IN ('approving','cancelling')").fetchall():
                                try:
                                    if r['status']=='approving': await service.approve(r['id'],admin)
                                    else: await service.cancel(r['id'],admin)
                                except Exception as exc:
                                    log.warning('operation_retry_failed request_id=%s kind=%s',r['id'],type(exc).__name__)
                    for n in s.db.execute('SELECT * FROM notices WHERE sent=0 ORDER BY id LIMIT 20').fetchall():
                        try:
                            await bot.send_message(n['recipient'],n['text'])
                            s.db.execute('UPDATE notices SET sent=1 WHERE id=?',(n['id'],)); s.db.commit()
                        except Exception as exc:
                            from aiogram.exceptions import TelegramForbiddenError
                            log.warning('notice_failed kind=%s',type(exc).__name__)
                            if isinstance(exc,TelegramForbiddenError):
                                s.db.execute('UPDATE notices SET sent=1 WHERE id=?',(n['id'],)); s.db.commit()
                except Exception as exc: log.error('maintenance_failed kind=%s',type(exc).__name__)
                await asyncio.sleep(20)
        web_runner=None
        if os.getenv('MINIAPP_URL'):
            from web_api import start_web
            web_runner=await start_web(s,service,operation_lock,token,int(os.getenv('MINIAPP_PORT','8087')))
            log.info('miniapp_started')
        worker=asyncio.create_task(maintenance())
        try:
            # Keep the HTTP interface available during a temporary Telegram outage.
            for attempt in range(5):
                try:
                    me=await bot.get_me()
                    break
                except Exception as exc:
                    log.warning('telegram_connect_retry kind=%s',type(exc).__name__)
                    if attempt==4: raise
                    await asyncio.sleep(min(5*(attempt+1),20))
            log.info('polling_started username=%s',me.username)
            await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
        finally:
            worker.cancel()
            try: await worker
            except asyncio.CancelledError: pass
            if web_runner: await web_runner.cleanup()
            await cal.close(); s.db.close(); Path('data/bot.pid').unlink(missing_ok=True)

if __name__=='__main__':
    Path('data').mkdir(exist_ok=True)
    handler=TimedRotatingFileHandler('data/bot.log',when='midnight',backupCount=30,encoding='utf-8')
    logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(levelname)s %(message)s',handlers=[handler])
    log.setLevel(logging.INFO)
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as exc:
        log.error('startup_failed kind=%s',type(exc).__name__); raise SystemExit('Не удалось запустить бота. Смотрите data/bot.log.') from None
