"""Google Calendar REST adapter. No mock success in live mode."""
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import httpx

class CalendarError(Exception): pass

def iso(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
def secure_json(path,data):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp')
    fd=os.open(str(tmp),os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f: json.dump(data,f)
    os.replace(tmp,p); p.chmod(0o600)

class GoogleCalendar:
    def __init__(self,calendar_id='primary',token_path='data/google_token.json',client_path='data/google_client.json'):
        self.id=calendar_id; self.token_path=Path(token_path); self.client_path=Path(client_path)
        self.client=httpx.AsyncClient(timeout=30); self.lock=asyncio.Lock()
    async def close(self): await self.client.aclose()
    def configured(self): return self.token_path.exists() and self.client_path.exists()
    async def access(self):
        async with self.lock:
            if not self.configured(): raise CalendarError('Google Календарь не подключён. Администратору нужно выполнить подключение по инструкции README.')
            token=json.loads(self.token_path.read_text()); config=json.loads(self.client_path.read_text())
            config=config.get('installed',{})
            if token.get('expires_at',0)<time.time()+90:
                if not token.get('refresh_token'): raise CalendarError('Требуется повторное подключение Google.')
                resp=await self.client.post('https://oauth2.googleapis.com/token',data=dict(client_id=config['client_id'],client_secret=config['client_secret'],refresh_token=token['refresh_token'],grant_type='refresh_token'))
                if resp.status_code!=200: raise CalendarError('Google не обновил авторизацию. Подключите аккаунт заново.')
                update=resp.json(); token.update(update); token['expires_at']=time.time()+update.get('expires_in',3600); secure_json(self.token_path,token)
            return token['access_token']
    async def request(self,method,path,**kwargs):
        token=await self.access()
        response=await self.client.request(method,'https://www.googleapis.com/calendar/v3/calendars/'+quote(self.id,safe='')+path,headers={'Authorization':'Bearer '+token},**kwargs)
        if response.status_code>=400: raise CalendarError(f'Google Calendar: HTTP {response.status_code}. Операция не подтверждена.')
        return response.json() if response.content else {}
    async def busy(self,start,end):
        page=None; out=[]
        while True:
            params=dict(timeMin=iso(start),timeMax=iso(end),singleEvents='true',maxResults=2500)
            if page: params['pageToken']=page
            result=await self.request('GET','/events',params=params)
            tz=result.get('timeZone','UTC')
            from zoneinfo import ZoneInfo
            for e in result.get('items',[]):
                if e.get('status')=='cancelled' or e.get('transparency')=='transparent': continue
                def stamp(value):
                    if 'dateTime' in value: return datetime.fromisoformat(value['dateTime'].replace('Z','+00:00')).timestamp()
                    return datetime.fromisoformat(value['date']).replace(tzinfo=ZoneInfo(tz)).timestamp()
                out.append((stamp(e['start']),stamp(e['end']),e['id']))
            page=result.get('nextPageToken')
            if not page: return out
    async def upsert(self,r,parent=None):
        eid=parent['event_id'] if parent else r['event_id']
        # GET resolves an uncertain earlier insertion without creating duplicates.
        if not parent:
            token=await self.access()
            check=await self.client.get('https://www.googleapis.com/calendar/v3/calendars/'+quote(self.id,safe='')+'/events/'+eid,headers={'Authorization':'Bearer '+token})
            if check.status_code==200:
                event=check.json()
                if event.get('status')=='cancelled': raise CalendarError('Ранее созданное событие отменено. Требуется проверка.')
                return await self.meet_ready(event)
            if check.status_code!=404: raise CalendarError(f'Не удалось проверить событие: HTTP {check.status_code}.')
        desc=(r['description'] or '')+f"\nУчастник: {r['name']}\nTelegram: "+('@'+r['username'] if r['username'] else 'Без username')+f"\nЗаявка №{r['id']}"
        body=dict(summary=r['title'],description=desc,start={'dateTime':iso(r['start'])},end={'dateTime':iso(r['end'])},
                  attendees=[{'email':r['email']}],reminders={'useDefault':False,'overrides':[{'method':'popup','minutes':60}]},
                  extendedProperties={'private':{'booking_id':str(r['id'])}})
        if not parent:
            body['id']=eid
            body['conferenceData']={'createRequest':{'requestId':eid,'conferenceSolutionKey':{'type':'hangoutsMeet'}}}
        event=await self.request('PATCH' if parent else 'POST','/events/'+eid if parent else '/events',params={'sendUpdates':'all','conferenceDataVersion':1},json=body)
        return await self.meet_ready(event)
    async def meet_ready(self,event):
        for attempt in range(5):
            if event.get('hangoutLink'): return event
            status=event.get('conferenceData',{}).get('createRequest',{}).get('status',{}).get('statusCode')
            if status=='failure': raise CalendarError('Событие создано, но Google Meet не создан. Требуется проверка администратора.')
            await asyncio.sleep(1)
            event=await self.request('GET','/events/'+event['id'])
        raise CalendarError('Событие существует, Google Meet ещё не готов. Повторите согласование позже; дубль не создаётся.')
    async def delete(self,eid):
        token=await self.access()
        response=await self.client.delete('https://www.googleapis.com/calendar/v3/calendars/'+quote(self.id,safe='')+'/events/'+eid,headers={'Authorization':'Bearer '+token},params={'sendUpdates':'all'})
        if response.status_code not in (200,204,404,410): raise CalendarError(f'Отмена не подтверждена Google: HTTP {response.status_code}.')
