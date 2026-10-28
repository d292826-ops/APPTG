"""Loopback-only demo. No dotenv, real tokens, Google credentials or notifications."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo
from aiohttp import web
from core import Store, Scheduler
from web_api import create_app

class DemoCalendar:
    def __init__(self): self.events={}
    def configured(self): return True
    async def busy(self,start,end): return [(v['start'],v['end'],k) for k,v in self.events.items() if v['start']<end and v['end']>start]
    async def upsert(self,r,parent=None):
        eid=parent['event_id'] if parent else r['event_id']
        self.events[eid]=r
        return {'id':eid,'hangoutLink':''}
    async def delete(self,eid): self.events.pop(eid,None)

async def build_demo(path):
    store=Store(str(path)); store.set('admin_id',1001)
    store.profile(1001,name='Демо Администратор',username='demo_admin',email='admin@example.com')
    store.profile(1002,name='Александр Петров',username='demo_user',email='alex@example.com')
    cal=DemoCalendar(); scheduler=Scheduler(store,cal)
    if not store.db.execute('SELECT 1 FROM requests').fetchone():
        today=datetime.now(ZoneInfo('Asia/Kamchatka')).date()
        days=[today+timedelta(days=i) for i in range(2,15) if (today+timedelta(days=i)).weekday()<5]
        for i,(title,duration) in enumerate([('Обсудить финансовую модель',30),('Планирование следующего месяца',45)]):
            slots=await scheduler.available(days[i],duration)
            if slots:
                r=await scheduler.submit(1002,dict(title=title,duration=duration,start=slots[0],name='Александр Петров',email='alex@example.com',description='Разобрать вопросы и согласовать следующие шаги.'))
                if i==1: await scheduler.approve(r['id'],1001)
    # Rehydrate only this demo's events when a caller supplies a persistent demo path.
    for row in store.db.execute("SELECT * FROM requests WHERE status='approved'"):
        cal.events[row['event_id']]=dict(row)
    app=create_app(store,scheduler,asyncio.Lock(),demo=True)
    async def close(app): store.db.close()
    app.on_cleanup.append(close)
    return app

if __name__=='__main__':
    # Temporary, separate database is discarded after this local demo exits.
    with tempfile.TemporaryDirectory(prefix='googlebot-demo-') as directory:
        print('Пользователь: http://127.0.0.1:8088/meet/demo/user',flush=True)
        print('Администратор: http://127.0.0.1:8088/meet/demo/admin',flush=True)
        web.run_app(build_demo(Path(directory)/'demo.sqlite3'),host='127.0.0.1',port=8088,access_log=None)
