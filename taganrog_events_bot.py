import os
import requests
import json
import random
from datetime import datetime, time, timedelta
from bs4 import BeautifulSoup
from telegram import Bot
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

events_db = []

def parse_yandex_afisha():
    """Парсит Яндекс Афишу Таганрога"""
    try:
        url = "https://afisha.yandex.ru/taganrog"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        event_items = soup.find_all('div', class_=['EventCard', 'event-card', 'afisha-event'])
        
        if len(event_items) == 0:
            event_items = soup.find_all('a', href=True)[:10]
        
        for item in event_items[:5]:
            try:
                title_elem = item.find(['h3', 'h2', 'p', 'span'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    if len(title) > 5 and len(title) < 100:
                        events.append({
                            'title': title,
                            'source': 'Яндекс Афиша',
                            'date': 'ближайшие дни',
                            'price': 'от 200 ₽'
                        })
            except:
                continue
        
        return events[:3]
    except Exception as e:
        logger.error(f"Error parsing Yandex: {e}")
        return []

def get_fallback_events():
    """Возвращает список популярных событий Таганрога (если парсинг не сработал)"""
    fallback_events = [
        {
            'title': 'Выставка в Таганрогском художественном музее',
            'date': 'ежедневно, 10:00-18:00',
            'location': 'ул. Александровская, 70',
            'price': '350 ₽',
            'emoji': '🎨'
        },
        {
            'title': 'Посещение памятных мест Чехова',
            'date': 'по выходным, 14:00',
            'location': 'Дом-музей Чехова',
            'price': '200 ₽',
            'emoji': '📚'
        },
        {
            'title': 'Прогулка по набережной Таганрога',
            'date': 'ежедневно',
            'location': 'Азовское море',
            'price': 'Бесплатно',
            'emoji': '🌅'
        },
        {
            'title': 'Кинопоказ в местных кинотеатрах',
            'date': 'ежедневно',
            'location': 'Кинотеатр "Современник"',
            'price': 'от 250 ₽',
            'emoji': '🎬'
        },
        {
            'title': 'Концерт в ДК "Фестивальный"',
            'date': 'по выходам, 19:00',
            'location': 'Дворец культуры',
            'price': '300-500 ₽',
            'emoji': '🎵'
        },
        {
            'title': 'Мастер-класс по рукоделию',
            'date': 'по средам, 18:00',
            'location': 'ЦКИ Таганрога',
            'price': '100 ₽',
            'emoji': '🎨'
        }
    ]
    return random.sample(fallback_events, 3)

def format_event_for_max(event):
    """Форматирует событие для поста в MAX"""
    emoji = event.get('emoji', '📍')
    title = event.get('title', 'Событие')
    date = event.get('date', 'ближайшие дни')
    location = event.get('location', 'Таганрог')
    price = event.get('price', 'Информация уточняется')
    
    formatted = f"""
{emoji} {title.upper()}

📅 {date}
📍 {location}

Интересно проводить время в городе? Приглашаем на это событие!

💰 {price}

#Таганрог #события #куданоджиди
"""
    return formatted.strip()

async def send_daily_events():
    """Отправляет события в Telegram в 9:00"""
    try:
        logger.info("Fetching events...")
        
        events = parse_yandex_afisha()
        if not events or len(events) < 2:
            logger.info("Using fallback events")
            events = get_fallback_events()
        
        if not events:
            events = get_fallback_events()
        
        bot = Bot(token=TELEGRAM_TOKEN)
        
        message = "📅 СОБЫТИЯ ТАГАНРОГА НА СЕГОДНЯ\n\n"
        message += f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n"
        message += "=" * 40 + "\n\n"
        
        selected_events = random.sample(events, min(3, len(events)))
        
        for i, event in enumerate(selected_events, 1):
            formatted = format_event_for_max(event)
            message += f"【 Событие {i} 】\n{formatted}\n\n"
            message += "─" * 40 + "\n\n"
        
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        
        logger.info("Events sent successfully")
        
    except Exception as e:
        logger.error(f"Error sending events: {e}")
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Ошибка при отправке событий: {str(e)}"
            )
        except:
            pass

async def main():
    """Запускает планировщик"""
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    scheduler.add_job(
        send_daily_events,
        'cron',
        hour=17,
        minute=57,
        timezone="Europe/Moscow"
    )
    
    scheduler.start()
    
    logger.info("Bot started. Scheduled for 9:00 Moscow time daily")
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
