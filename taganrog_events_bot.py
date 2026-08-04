import asyncio
import logging
import os
import sqlite3
import html
import io
import re
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin
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
EXCLUDED_PHONE_DIGITS = "8634610013"
GLOBAL_TICKET_URL = "vmuzey.com/museum/taganrogskiy-muzey-zapovednik"

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {"keys": ["литературный музей", "литературно-музыкальн"], "name": "Литературный музей А.П. Чехова", "address": "ул. Октябрьская, 9"},
    {"keys": ["дворец алфераки", "историко-краеведческ"], "name": "Историко-краеведческий музей (Дворец Алфераки)", "address": "ул. Фрунзе, 41"},
    {"keys": ["домик чехова"], "name": "Музей «Домик Чехова»", "address": "ул. Чехова, 69"},
    {"keys": ["лавка чеховых", "лавка чехова"], "name": "Музей «Лавка Чеховых»", "address": "ул. Александровская, 100"},
    {"keys": ["музей василенко", "василенко"], "name": "Музей И.Д. Василенко", "address": "ул. Чехова, 88"},
    {"keys": ["музей дурова", "дурова"], "name": "Музей А.А. Дурова", "address": "ул. А. Глушко, 44"},
    {"keys": ["градостроительства"], "name": "Музей градостроительства и быта", "address": "ул. Фрунзе, 80"},
    {"keys": ["юрнкц", "южно-российский"], "name": "ЮРНКЦ А.П. Чехова", "address": "ул. Октябрьская, 9"}
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
    hashtags: List[str] = field(default_factory=list)
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
def parse_event_date(date_text: str) -> Optional[date]:
    if not date_text: return None
    try:
        match = re.search(r'(\d{1,2})\s+([а-яА-Я]+)', date_text.lower())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            month = MONTH_MAP.get(month_str)
            if month:
                current_year = date.today().year
                event_date = date(current_year, month, day)
                if event_date < date.today() and date.today().month == 12 and month == 1:
                    event_date = date(current_year + 1, month, day)
                return event_date
    except Exception as e:
        logger.warning(f"Не удалось распарсить дату '{date_text}': {e}")
    return None

def extract_and_format_phones(text: str) -> List[str]:
    raw_phones = re.findall(r'(?:\+?7|8)?[\s(-]*\d{3,4}[\s)-]*\d{2,3}[\s-]*\d{2}[\s-]*\d{2}|\b\d{2}[\s-]?\d{2}[\s-]?\d{2}\b', text)
    valid_phones = []
    
    for p in raw_phones:
        clean_p = re.sub(r'\D', '', p)
        if EXCLUDED_PHONE_DIGITS in clean_p:
            continue
            
        if len(clean_p) == 11 and clean_p.startswith(('7', '8')):
            formatted = f"+7 ({clean_p[1:5]}) {clean_p[5:7]}-{clean_p[7:9]}-{clean_p[9:11]}"
            valid_phones.append(formatted)
        elif len(clean_p) == 10:
            formatted = f"+7 ({clean_p[0:4]}) {clean_p[4:6]}-{clean_p[6:8]}-{clean_p[8:10]}"
            valid_phones.append(formatted)
        elif len(clean_p) >= 6:
            valid_phones.append(p.strip())
            
    return list(set(valid_phones))

def detect_event_type(text_content: str) -> str:
    text_content = text_content.lower()
    if "мастер-класс" in text_content or "мастер класс" in text_content:
        return "Мастер-класс"
    elif "литературно-музыкальн" in text_content or "джаз" in text_content:
        return "Литературно-музыкальная программа"
    elif "экскурси" in text_content:
        return "Экскурсия"
    elif "выставк" in text_content or "экспозиц" in text_content:
        return "Выставка"
    elif "лекци" in text_content:
        return "Лекция"
    return ""

def format_event_post(event: Event) -> str:
    lines = []
    lines.append("МУЗЕЙНАЯ АФИША ТАГАНРОГА")
    
    if event.event_type:
        lines.append(f"<i>{html.escape(event.event_type)}</i>")
        
    lines.append(f"<b>{html.escape(event.title)}</b>\n")
    
    if event.date_str:
        lines.append(f"Дата: {html.escape(event.date_str)}")
    if event.time_str:
        lines.append(f"Время: {html.escape(event.time_str)}")
    if event.prices:
        lines.append(f"Цена: {html.escape(event.prices)}\n")
        
    # Блок записи и телефонов
    if event.requires_booking or event.event_type == "Мастер-класс":
        lines.append("<b><i>Предварительная запись обязательна!</i></b>")
        
    if event.phones:
        phones_str = ", ".join([html.escape(p) for p in event.phones])
        lines.append(f"Телефон: {phones_str}\n")
    elif event.requires_booking or event.event_type == "Мастер-класс":
        lines.append("")

    # Место проведения
    loc_parts = []
    if event.location: loc_parts.append(event.location)
    if event.address: loc_parts.append(event.address)
    if loc_parts:
        lines.append(f"Место: {html.escape(', '.join(loc_parts))}\n")
        
    # Индивидуальные хештеги
    if event.hashtags:
        lines.append(" ".join([html.escape(tag) for tag in event.hashtags]))
    else:
        lines.append("#Таганрог #АфишаТаганрог")
        
    return "\n".join(lines)

# ===================== ПАРСИНГ TGLIAMZ =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "event_type": "", "date_str": "", "parsed_date": None,
        "time_str": "", "location": "", "address": "", "prices": "",
        "requires_booking": False, "phones": [], "hashtags": [],
        "buy_ticket_url": "", "image_url": None, "is_shop": False
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with session.get(detail_url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                
                if any(word in html_text.lower() for word in STRICT_SOUVENIR_WORDS):
                    data["is_shop"] = True
                    return data
                    
                soup = BeautifulSoup(html_text, "html.parser")
                text_content = soup.get_text(separator=" ")
                text_lower = text_content.lower()
                
                data["event_type"] = detect_event_type(text_content)
                
                # Бронь и телефоны
                if "предварительная запись" in text_lower or "запись по телефону" in text_lower:
                    data["requires_booking"] = True
                data["phones"] = extract_and_format_phones(html_text)
                
                # Хештеги
                hashtags = re.findall(r'#[\wА-Яа-яЁё]+', text_content)
                if hashtags:
                    data["hashtags"] = list(dict.fromkeys(hashtags)) # Сохраняем уникальные с порядком
                    
                # Филиал
                for branch in MUSEUM_BRANCHES:
                    if any(key in text_lower for key in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        break
                        
                # Ссылка на билеты (Исключая общую ссылку музея)
                buy_btn = soup.find("a", href=re.compile(r'vmuzey|afisha|tickets', re.I))
                if buy_btn and buy_btn.get("href"):
                    if GLOBAL_TICKET_URL not in buy_btn["href"]:
                        data["buy_ticket_url"] = buy_btn["href"]
                        
                # Картинка
                img_tag = soup.find("img", src=re.compile(r'/upload/'))
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_tag["src"])
                    
                # Даты и время
                date_match = re.search(r'(?i)(?:дата|когда):\s*(\d{1,2}\s+[а-я]+)', text_content)
                if date_match:
                    data["date_str"] = date_match.group(1).title()
                    data["parsed_date"] = parse_event_date(data["date_str"])
                    
                time_match = re.search(r'(?i)(?:время|начало):\s*(\d{1,2}[:.-]\d{2})', text_content)
                if time_match:
                    data["time_str"] = time_match.group(1).replace('.', ':').replace('-', ':')

                price_match = re.search(r'(?i)(?:цена|стоимость|билет)[^0-9]*(\d{2,4})\s*(?:руб|р)', text_content)
                if price_match:
                    data["prices"] = f"{price_match.group(1)} руб. (в кассе музея)"
                    
    except Exception as e:
        logger.error(f"Ошибка при парсинге деталей {detail_url}: {e}")
        
    return data

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    seen_urls = set()
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                
                items = soup.find_all("div", class_=re.compile(r'event|calendar-item|afisha|news-item', re.I))
                logger.info(f"Найдено блоков афиши на странице: {len(items)}")
                
                for item in items:
                    title = ""
                    event_url = ""
                    
                    # Проходим по всем ссылкам внутри блока, чтобы не застрять на пустых картинках
                    for a_tag in item.find_all("a", href=True):
                        text = a_tag.get_text(strip=True)
                        if text:
                            title = text
                            event_url = urljoin(base_url, a_tag["href"])
                            break
                            
                    # Отсеиваем "Афишу выходного дня", отсутствие названия или дубликаты
                    if not title or "афиша выходного дня" in title.lower():
                        continue
                        
                    if event_url in seen_urls:
                        continue
                    seen_urls.add(event_url)
                        
                    if is_event_sent(event_url):
                        continue
                        
                    detail_data = await parse_tgliamz_detail(session, event_url)
                    
                    if detail_data.get("is_shop"):
                        continue
                        
                    # Отсеиваем прошедшие события
                    if detail_data.get("parsed_date") and detail_data["parsed_date"] < date.today():
                        continue
                        
                    event = Event(
                        title=title,
                        url=event_url,
                        event_type=detail_data.get("event_type", ""),
                        date_str=detail_data.get("date_str", ""),
                        parsed_date=detail_data.get("parsed_date"),
                        time_str=detail_data.get("time_str", ""),
                        location=detail_data.get("location", ""),
                        address=detail_data.get("address", ""),
                        prices=detail_data.get("prices", ""),
                        requires_booking=detail_data.get("requires_booking", False),
                        phones=detail_data.get("phones", []),
                        hashtags=detail_data.get("hashtags", []),
                        buy_ticket_url=detail_data.get("buy_ticket_url", ""),
                        image_url=detail_data.get("image_url")
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
        keyboard.append([InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)])
        
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
                    await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            
        mark_event_sent(event.url)
        logger.info(f"Успешно отправлено событие: {event.title}")
        await asyncio.sleep(1.5)
        
    except TelegramError as e:
        logger.error(f"Ошибка Telegram при отправке '{event.title}': {e}")

# ===================== ОСНОВНАЯ ТОЧКА ВХОДА =====================
async def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    user_id_str = os.environ.get("TELEGRAM_USER_ID") or os.getenv("CHAT_ID")
    
    if not token or not user_id_str:
        logger.error("ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены!")
        return
        
    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.error("Ошибка: CHAT_ID должен быть числовым значением.")
        return

    init_db()
    bot = Bot(token=token)
    
    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await parse_tgliamz_museums(session)
        
        # Строгая хронологическая сортировка перед отправкой (от ранних к поздним)
        events.sort(key=lambda x: (x.parsed_date or date.max, x.time_str))
        
        logger.info(f"Найдено новых событий для отправки: {len(events)}")
        
        for event in events:
            await send_event_to_telegram(bot, user_id, event, session)

if __name__ == "__main__":
    asyncio.run(main())
