"""Persistent scheduling rules; independent of the Telegram interface."""
import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULTS = dict(timezone='Asia/Kamchatka', weekdays=[0,1,2,3,4], start='09:00', end='18:00',
                min_hours=24, horizon_days=30, buffer_before=0, buffer_after=0, daily_limit=4,
                approval_hours=48, closed=[])
DURATIONS = (15,30,45,60,90)
ACTIVE = ('pending','approving','approved','cancelling')
LABELS = dict(pending='На согласовании', approving='Создаётся событие', approved='Согласована',
              cancelling='Выполняется отмена', rejected='Отклонена', expired='Истёк срок',
              cancelled='Отменена', moved='Перенесена', completed='Завершена', conflict='Время занято')

class RuleError(Exception): pass

class Store:
    def __init__(self, path='data/booking.sqlite3'):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, email TEXT, banned INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT, username TEXT, email TEXT,
          title TEXT, description TEXT, start REAL, end REAL, created REAL, deadline REAL, status TEXT,
          parent INTEGER, event_id TEXT, meet TEXT, comment TEXT, closed REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY, recipient INTEGER, text TEXT, sent INTEGER DEFAULT 0);
        ''')
        self.db.commit()
    def get(self, key, default=None):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))
        self.db.commit()
    def settings(self): return dict(DEFAULTS, **self.get('settings',{}))
    def row(self,n):
        r=self.db.execute('SELECT * FROM requests WHERE id=?',(n,)).fetchone()
        if not r: raise RuleError('Заявка не найдена.')
        return dict(r)
    def user(self, uid):
        r=self.db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        return dict(r) if r else dict(id=uid,name='',username='',email='',banned=0)
    def profile(self,uid,name=None,username=None,email=None):
        old=self.user(uid)
        self.db.execute('INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)',
                        (uid,old['name'] if name is None else name,old['username'] if username is None else username,
                         old['email'] if email is None else email,old['banned']))
        self.db.commit()
    def session(self,uid,value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)',(uid,json.dumps(value)))
            self.db.commit()
        r=self.db.execute('SELECT data FROM sessions WHERE user_id=?',(uid,)).fetchone()
        return json.loads(r[0]) if r else {}
    def update(self,n,**values):
        values['updated']=time.time()
        self.db.execute('UPDATE requests SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),n))
        self.db.commit()
    def notice(self,uid,text):
        self.db.execute('INSERT INTO notices(recipient,text) VALUES (?,?)',(uid,text))
        self.db.commit()
    def both(self,r,text):
        for uid in set([r['user_id'],self.get('admin_id')]):
            if uid: self.notice(uid,text)
    def owned(self,n,uid):
        r=self.row(n)
        if uid not in (r['user_id'], self.get('admin_id')): raise RuleError('Нет доступа к заявке.')
        return r
    def admin(self,uid):
        if uid!=self.get('admin_id'): raise RuleError('Доступ только администратору.')
    def cleanup(self,now=None):
        now=now or time.time()
        for row in self.db.execute("SELECT * FROM requests WHERE status='pending' AND deadline<=?",(now,)).fetchall():
            r=dict(row); self.update(r['id'],status='expired',closed=now)
            self.both(r,f"Срок согласования заявки №{r['id']} истёк. Резерв освобождён.")
        self.db.execute("UPDATE requests SET status='completed',closed=end WHERE status='approved' AND end<=?",(now,))
        cutoff=now-30*86400
        self.db.execute('DELETE FROM requests WHERE closed IS NOT NULL AND closed<?',(cutoff,))
        self.db.execute('DELETE FROM notices WHERE sent=1')
        self.db.commit()

class Scheduler:
    def __init__(self,store,calendar):
        self.s=store; self.cal=calendar; self.lock=asyncio.Lock()
    def stamp(self,t):
        return datetime.fromtimestamp(t,ZoneInfo(self.s.settings()['timezone'])).strftime('%d.%m.%Y %H:%M')
    def card(self,r):
        s=self.s.settings()
        return (f"Заявка №{r['id']} · {LABELS[r['status']]}\n{r['title']}\n"
                f"{self.stamp(r['start'])} — {self.stamp(r['end'])}\nЧасовой пояс: {s['timezone']}\n"
                f"{r['name']} · {'@'+r['username'] if r['username'] else 'Без username'}\n"
                f"Почта: {r['email']}\nОписание: {r['description'] or '—'}\n"
                +(f"Google Meet: {r['meet']}\n" if r.get('meet') else '')
                +(f"Комментарий: {r['comment']}\n" if r.get('comment') else ''))
    async def available(self,day,duration,parent=None,exclude=None,now=None,for_approval=False):
        s=self.s.settings(); tz=ZoneInfo(s['timezone']); now=now or time.time()
        d=datetime.fromisoformat(str(day)).date(); today=datetime.fromtimestamp(now,tz).date()
        if duration not in DURATIONS or d<today or d>today+timedelta(days=s['horizon_days']) or d.weekday() not in s['weekdays']: return []
        begin=datetime.combine(d,datetime.min.time(),tzinfo=tz).timestamp(); finish=begin+25*3600
        if any(x.get('date')==str(d) and not x.get('start') for x in s['closed']): return []
        events=await self.cal.busy(begin,finish)
        rows=[dict(r) for r in self.s.db.execute("SELECT * FROM requests WHERE status IN ('pending','approving','approved','cancelling')").fetchall()]
        rows=[r for r in rows if r['id'] not in (parent,exclude) and not (r['status']=='pending' and r['deadline']<=now)]
        count=sum(datetime.fromtimestamp(r['start'],tz).date()==d for r in rows)
        if count>=s['daily_limit']: return []
        parent_row=self.s.row(parent) if parent else None
        ignore_id=parent_row['event_id'] if parent_row else None
        occupied=[(a,b) for a,b,eid in events if eid!=ignore_id or not ignore_id]
        occupied += [(r['start'],r['end']) for r in rows]
        occupied = [(a-s['buffer_before']*60,b+s['buffer_after']*60) for a,b in occupied]
        for x in s['closed']:
            if x.get('date')==str(d) and x.get('start'):
                occupied.append((datetime.fromisoformat(f"{d}T{x['start']}").replace(tzinfo=tz).timestamp(),
                                 datetime.fromisoformat(f"{d}T{x['end']}").replace(tzinfo=tz).timestamp()))
        out=[]
        work_start=datetime.fromisoformat(f"{d}T{s['start']}").replace(tzinfo=tz).timestamp()
        work_end=datetime.fromisoformat(f"{d}T{s['end']}").replace(tzinfo=tz).timestamp()
        for minute in range(0,24*60,30):
            a=datetime.combine(d,datetime.min.time(),tzinfo=tz).timestamp()+minute*60; b=a+duration*60
            left=a-s['buffer_before']*60; right=b+s['buffer_after']*60
            if a<now+(0 if for_approval else s['min_hours'])*3600 or left<work_start or right>work_end: continue
            if any(left<y and right>x for x,y in occupied): continue
            out.append(a)
        return out
    async def submit(self,uid,draft,operation=None):
        async with self.lock:
            self.s.cleanup(); u=self.s.user(uid)
            if u['banned']: raise RuleError('Создание новых заявок недоступно.')
            if not self.s.get('admin_id'): raise RuleError('Администратор ещё не подключён.')
            if draft.get('parent'):
                p=self.s.owned(draft['parent'],uid)
                if p['status']!='approved': raise RuleError('Перенести можно только согласованную встречу.')
                if self.s.db.execute("SELECT 1 FROM requests WHERE parent=? AND status IN ('pending','approving')",(p['id'],)).fetchone(): raise RuleError('Заявка на перенос уже существует.')
            a=draft['start']; day=datetime.fromtimestamp(a,ZoneInfo(self.s.settings()['timezone'])).date()
            if a not in await self.available(day,draft['duration'],parent=draft.get('parent')): raise RuleError('Время больше недоступно. Выберите новый слот.')
            now=time.time(); settings=self.s.settings(); deadline=min(now+settings['approval_hours']*3600,a)
            self.s.profile(uid,name=draft['name'],email=draft['email'])
            v=dict(user_id=uid,name=draft['name'],username=u['username'],email=draft['email'],title=draft['title'],
                   description=draft.get('description',''),start=a,end=a+draft['duration']*60,created=now,
                   deadline=deadline,status='pending',parent=draft.get('parent'),event_id=uuid.uuid4().hex,updated=now)
            cur=self.s.db.execute('INSERT INTO requests ('+','.join(v)+') VALUES ('+','.join('?' for _ in v)+')',tuple(v.values()))
            number=cur.lastrowid
            if operation:
                key,fingerprint=operation
                self.s.db.execute('INSERT INTO web_operations VALUES (?,?,?,?,?)',(uid,key,fingerprint,number,now))
            self.s.db.commit(); r=self.s.row(number)
            self.s.notice(self.s.get('admin_id'),'Новая заявка\n'+self.card(r)+f"Согласовать до {self.stamp(deadline)}.\nОткройте /admin")
            return r
    async def approve(self,n,uid):
        self.s.admin(uid)
        async with self.lock:
            self.s.cleanup(); r=self.s.row(n)
            if r['status']=='approved': return r
            if r['status'] not in ('pending','approving'): raise RuleError('Заявка уже закрыта.')
            if r['status']=='pending':
                day=datetime.fromtimestamp(r['start'],ZoneInfo(self.s.settings()['timezone'])).date()
                if r['start'] not in await self.available(day,int((r['end']-r['start'])/60),parent=r['parent'],exclude=n,for_approval=True):
                    self.s.update(n,status='conflict',closed=time.time()); self.s.both(r,f'Время заявки №{n} больше недоступно. Выберите другой слот.'); raise RuleError('Время занято.')
                self.s.update(n,status='approving')
            # Persist operation intent before the external request. Stable event IDs make retries safe.
            parent=self.s.row(r['parent']) if r['parent'] else None
            if parent and parent['status']!='approved': raise RuleError('Исходная встреча уже недоступна. Нужна проверка администратора.')
            event=await self.cal.upsert(r,parent)
            self.s.db.execute("UPDATE requests SET status='approved',event_id=?,meet=?,updated=? WHERE id=?",(event['id'],event.get('hangoutLink',''),time.time(),n))
            if parent: self.s.db.execute("UPDATE requests SET status='moved',closed=?,updated=? WHERE id=?",(time.time(),time.time(),parent['id']))
            self.s.db.commit()
            result=self.s.row(n); self.s.both(result,'Встреча согласована. Приглашение отправлено на почту.\n'+self.card(result))
            return result
    async def reject(self,n,uid,comment=''):
        self.s.admin(uid)
        async with self.lock:
            r=self.s.row(n)
            if r['status']!='pending': raise RuleError('Отклонить можно заявку на согласовании.')
            self.s.update(n,status='rejected',comment=comment,closed=time.time())
            self.s.both(r,f'Заявка №{n} отклонена. '+comment)
    async def cancel(self,n,uid):
        async with self.lock:
            r=self.s.owned(n,uid)
            if r['status'] not in ('pending','approved','cancelling'): raise RuleError('Отмена сейчас недоступна.')
            if self.s.db.execute("SELECT 1 FROM requests WHERE parent=? AND status='approving'",(n,)).fetchone(): raise RuleError('Сначала завершите выполняющийся перенос.')
            if r['status'] in ('approved','cancelling'):
                self.s.update(n,status='cancelling'); await self.cal.delete(r['event_id'])
            self.s.update(n,status='cancelled',closed=time.time())
            for x in self.s.db.execute("SELECT id FROM requests WHERE parent=? AND status='pending'",(n,)).fetchall():
                self.s.update(x['id'],status='cancelled',closed=time.time())
            self.s.both(r,f'Заявка №{n} отменена. Связанное событие отменено, если было создано.')
    async def ban(self,uid,admin,banned=True):
        self.s.admin(admin)
        async with self.lock:
            self.s.profile(uid); self.s.db.execute('UPDATE users SET banned=? WHERE id=?',(int(banned),uid)); self.s.db.commit()
            if banned:
                for x in self.s.db.execute("SELECT * FROM requests WHERE user_id=? AND status='pending'",(uid,)).fetchall():
                    r=dict(x); self.s.update(r['id'],status='rejected',closed=time.time()); self.s.both(r,f"Заявка №{r['id']} отклонена.")
