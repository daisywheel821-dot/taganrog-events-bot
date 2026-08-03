import asyncio
import logging
import os
import sqlite3
import html
import io
import re
from datetime import datetime, date
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from urllib.parse import urljoin, urlparse
import aiohttp
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ===================== НАСТРОЙКА ЛОГИРОВАНИЯ =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== КОНФИГУРАЦИЯ И КОНСТАНТЫ =====================
DB_PATH = "data/taganrog_events.db"

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры", 
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
]

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {"keys": ["литературный музей", "литературно-музыкальн"], "tag": "#ЛитературныйМузей"},
    {"keys": ["дворец алфераки", "историко-краеведческ"], "tag": "#ДворецАлфераки"},
    {"keys": ["домик чехова"], "tag": "#ДомикЧехова"},
    {"keys": ["лавка чеховых"], "tag": "#ЛавкаЧеховых"},
    {"keys": ["музей василенко"], "tag": "#МузейВасиленко"},
    {"keys": ["музей дурова"], "tag": "#МузейДурова"},
    {"keys": ["градостроительства"], "tag": "#МузейГрадостроительства"}
]

@dataclass
class Event:
    title: str
    url: str
    event_type: str = ""
    date_str: str = ""
    parsed_date: Optional[date] = None
    time_str: str = ""
    location: str = ""
    address: str = ""
    prices: str = ""
    requires_booking: bool = False
    phones: List[str] = field(default_factory=list)
    branch_tag: str = ""
    buy_ticket_url: str = ""
    image_url: Optional[str] = None

# ===================== БАЗА ДАННЫХ SQLite =====================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            url TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_events WHERE url = ?", (url,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_event_sent(url: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_events (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_souvenir_shop_item(html_text: str) -> bool:
    text_lower = html_text.lower()
    for word in STRICT_SOUVENIR_WORDS:
        if word in text_lower:
            return True
    return False

def format_event_post(event: Event) -> str:
    lines = []
    
    if event.branch_tag:
        lines.append(f"<b>{html.escape(event.branch_tag)}</b>\n")
    
    lines.append(f"<b>{html.escape(event.title)}</b>\n")
    
    if event.date_str:
        date_line = f"📅 <b>Дата:</b> {html.escape(event.date_str)}"
        if event.time_str:
            date_line += f" | ⏰ {html.escape(event.time_str)}"
        lines.append(date_line)
        
    if event.location:
        lines.append(f"📍 <b>Место:</b> {html.escape(event.location)}")
    if event.address:
        lines.append(f"🏛️ <b>Адрес:</b> {html.escape(event.address)}")
    if event.prices:
        lines.append(f"💰 <b>Билеты:</b> {html.escape(event.prices)}")
        
    if event.requires_booking:
        lines.append("\n⚠️ <b>Предварительная запись обязательна!</b>")
        
    if event.phones:
        phones_str = ", ".join([html.escape(p) for p in event.phones])
        lines.append(f"📞 <b>Запись / Справки:</b> {phones_str}")
        
    return "\n".join(lines)

# ===================== ПАРСИНГ TGLIAMZ =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "event_type": "", "date_str": "", "parsed_date": None,
        "time_str": "", "location": "", "address": "", "prices": "",
        "requires_booking": False, "phones": [], "branch_tag": "",
        "buy_ticket_url": "", "image_url": None, "is_shop": False
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                
                soup = BeautifulSoup(html_text, "html.parser")
                
                # Извлечение телефонов (без исключения общего номера)
                phones = re.findall(r'(?:\+7|8)[\s\-]?\(?\d{3,4}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', html_text)
                data["phones"] = list(set(phones))
                
                # Поиск ссылки на покупку билета
                buy_btn = soup.find("a", href=re.compile(r'vmuzey|afisha|tickets', re.I))
                if buy_btn and buy_btn.get("href"):
                    data["buy_ticket_url"] = buy_btn["href"]
                    
                # Картинка события
                img_tag = soup.find("img", src=re.compile(r'/upload/'))
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_tag["src"])
                    
    except Exception as e:
        logger.error(f"Ошибка при парсинге деталей {detail_url}: {e}")
    return data

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    try:
        async with session.get(url, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                
                # Поиск афишных блоков
                items = soup.find_all("div", class_=re.compile(r'event|calendar-item|afisha', re.I))
                for item in items:
                    link = item.find("a", href=True)
                    if not link:
                        continue
                    event_url = urljoin("https://tgliamz.ru", link["href"])
                    title = link.get_text(strip=True)
                    
                    if not title or is_event_sent(event_url):
                        continue
                    
                    detail_data = await parse_tgliamz_detail(session, event_url)
                    if detail_data.get("is_shop"):
                        continue
                        
                    event = Event(
                        title=title,
                        url=event_url,
                        image_url=detail_data.get("image_url"),
                        buy_ticket_url=detail_data.get("buy_ticket_url"),
                        phones=detail_data.get("phones", [])
                    )
                    events.append(event)
    except Exception as e:
        logger.error(f"Ошибка при парсинге календаря TGLIAMZ: {e}")
    return events

# ===================== ОТПРАВКА В TELEGRAM =====================
async def send_event_to_telegram(bot: Bot, user_id: int, event: Event, session: aiohttp.ClientSession):
    text = format_event_post(event)
    
    keyboard = []
    if event.buy_ticket_url:
        keyboard.append([InlineKeyboardButton("🎟️ Купить билет", url=event.buy_ticket_url)])
    keyboard.append([InlineKeyboardButton("🔗 Источник события", url=event.url)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    try:
        if event.image_url:
            async with session.get(event.image_url, timeout=10) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=io.BytesIO(image_bytes),
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
        else:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            
        mark_event_sent(event.url)
        logger.info(f"Успешно отправлено событие: {event.title}")
        await asyncio.sleep(1.5) # Пауза между сообщениями
        
    except TelegramError as e:
        logger.error(f"Ошибка Telegram при отправке '{event.title}': {e}")

# ===================== ОСНОВНОЙ ТОЧКА ВХОДА =====================
async def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    user_id_str = os.environ.get("TELEGRAM_USER_ID")

    if not token or not user_id_str:
        logger.error("Ошибка: Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_USER_ID в переменных окружения.")
        return

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.error("Ошибка: TELEGRAM_USER_ID должен быть числовым значениями.")
        return

    init_db()
    bot = Bot(token=token)

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await parse_tgliamz_museums(session)
        logger.info(f"Найдено новых событий: {len(events)}")

        for event in events:
            await send_event_to_telegram(bot, user_id, event, session)

if __name__ == "__main__":
    asyncio.run(main())
