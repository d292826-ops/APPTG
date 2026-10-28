import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot
from google_calendar import GoogleCalendar
import time
async def main():
    load_dotenv()
    async with Bot(os.environ['TELEGRAM_BOT_TOKEN']) as b:
        me=await b.get_me(); print('Telegram: @'+me.username)
        hook=await b.get_webhook_info(); print('Webhook: '+('активен — polling запускать нельзя' if hook.url else 'не установлен; polling доступен'))
    cal=GoogleCalendar(os.getenv('GOOGLE_CALENDAR_ID','primary'))
    try:
        await cal.busy(time.time(),time.time()+86400); print('Google Календарь: доступ проверен')
    except Exception as exc: print('Google Календарь: '+str(exc))
    finally: await cal.close()
if __name__=='__main__': asyncio.run(main())
