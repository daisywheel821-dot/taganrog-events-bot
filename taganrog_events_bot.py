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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ===================== НАСТРОЙКА ЛОГИРОВАНИЯ =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры",
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
]

# Исключаем общий справочный номер музея из шапки/подвала
EXCLUDED_PHONE_DIGITS = "8634610013"

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн"],
        "name": "Литературный музей\nА.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЛитературныйМузейЧехова"
    },
    {
        "keys": ["юрнкц", "южно-российский"],
        "name": "ЮРНКЦ\nА.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЮРНКЦЧехова"
    },
    {
        "keys": ["дворец алфераки", "историко-краеведческий"],
        "name": "Историко-краеведческий музей\n(Дворец Алфераки)",
        "address": "ул. Фрунзе, 41",
        "tag": "#ДворецАлфераки"
    },
    {
        "keys": ["домик чехова"],
        "name": "Музей «Домик Чехова»",
        "address": "ул. Чехова, 69",
        "tag": "#ДомикЧехова"
    },
    {
        "keys": ["лавка чеховых", "лавка чехова"],
        "name": "Музей «Лавка Чеховых»",
        "address": "ул. Александровская, 100",
        "tag": "#ЛавкаЧеховых"
    },
    {
        "keys": ["градостроительства"],
        "name": "Музей градостроительства и быта",
        "address": "ул. Фрунзе, 80",
        "tag": "#МузейГрадостроительства"
    },
    {
        "keys": ["дурова"],
        "name": "Музей А.А. Дурова",
        "address": "ул. А. Глушко, 44",
        "tag": "#МузейДурова"
    },
    {
        "keys": ["василенко"],
        "name": "Музей И.Д. Василенко",
        "address": "ул. Чехова, 88",
        "tag": "#МузейВасиленко"
    }
]

# ===================== МОДЕЛИ ДАННЫХ =====================

class Category(Enum):
    THEATRE_MONTH = "THEATRE_MONTH"
    MUSEUM = "MUSEUM"

@dataclass
class Event:
    event_id: str
    category: Category
    title: str
    event_type: str = ""
    date_str: str = ""
    parsed_date: Optional[date] = None
    time_str: str = ""
    location: str = ""
    address: str = ""
    prices: str = ""
    requires_booking: bool = False
    phones: List[tuple] = field(default_factory=list)
    tickets_url: str = ""
    buy_ticket_url: str = ""
    image_url: Optional[str] = None
    tags: List[str] = field(default_factory=list)

# ===================== БАЗА ДАННЫХ =====================

class Database:
    def __init__(self, db_path: str = "data/taganrog_events.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_events (
                    event_id TEXT PRIMARY KEY,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def is_sent(self, event_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM sent_events WHERE event_id = ?", (event_id,))
            return cursor.fetchone() is not None

    def mark_as_sent(self, event_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO sent_events (event_id) VALUES (?)", (event_id,))
            conn.commit()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def is_souvenir_shop_item(text: str) -> bool:
    check_str = text.lower()
    return any(word in check_str for word in STRICT_SOUVENIR_WORDS)

def parse_event_date(date_text: str) -> Optional[date]:
    if not date_text:
        return None
    
    clean_text = date_text.lower().strip()
    match = re.search(r'(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?', clean_text)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)
        year = int(match.group(3)) if match.group(3) else datetime.now().year
        
        if month_str in MONTH_MAP:
            month = MONTH_MAP[month_str]
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None

def extract_image_url(soup: BeautifulSoup, base_url: str = "https://tgliamz.ru") -> Optional[str]:
    img_tag = soup.select_one(".news-item-img img")
    if img_tag and img_tag.get("src"):
        return urljoin(base_url, img_tag["src"])
    return None

def extract_targeted_phones(text_block: str) -> List[tuple]:
    """Извлекает телефоны из текста, исключая общий номер музея."""
    phone_pattern = r'(?:\+?7|8)?[\s\(-]*\d{3,4}[\s\)-]*\d{2,3}[\s-]*\d{2}[\s-]*\d{2}|\b\d{2}[\s-]?\d{2}[\s-]?\d{2}\b'
    matches = re.finditer(phone_pattern, text_block)
    results = []
    
    for match in matches:
        raw_phone = match.group(0)
        digits = re.sub(r'\D', '', raw_phone)
        
        # Исключение общего телефона музея
        if EXCLUDED_PHONE_DIGITS and EXCLUDED_PHONE_DIGITS in digits:
            continue
            
        if len(digits) >= 6:
            if len(digits) == 6:
                formatted = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
                tel_link = f"+78634{digits}"
            elif len(digits) in (10, 11):
                if digits.startswith('8'):
                    digits = '7' + digits[1:]
                elif len(digits) == 10:
                    digits = '7' + digits
                formatted = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                tel_link = f"+{digits}"
            else:
                formatted = raw_phone
                tel_link = f"+{digits}"
                
            results.append((formatted, tel_link))
            
    return list(dict.fromkeys(results))

def detect_event_type(soup: BeautifulSoup, title: str, full_text: str) -> str:
    combined = (title + " " + full_text).lower()
    if "мастер-класс" in combined or "мастер класс" in combined:
        return "Мастер-класс"
    elif "литературно-музыкальн" in combined or "джаз" in combined or "концерт" in combined:
        return "Литературно-музыкальная программа"
    elif "музыкально-поэтич" in combined or "музыкальный вечер" in combined:
        return "Музыкальный вечер"
    elif "лекци" in combined:
        return "Лекция"
    elif "экскурси" in combined:
        return "Экскурсионный сеанс"
    elif "выставк" in combined or "экспозиц" in combined:
        return "Выставка"
    elif "спектакль" in combined:
        return "Спектакль"
    return "Мероприятие"

def check_requires_booking(text: str) -> bool:
    clean_text = " ".join(text.lower().split())
    keywords = [
        "предварительная запись обязательна", "предварительная запись",
        "запись по телефону", "бронирование мест обязательно",
        "бронирование мест", "обязательна предварительная запись",
        "по предварительной записи", "по предварительной брони"
    ]
    return any(kw in clean_text for kw in keywords)

def generate_museum_tags(text: str, branch_tag: str) -> List[str]:
    tags = ["#ТГЛИАМЗ"]
    if branch_tag:
        tags.append(branch_tag)
    tags.extend(["#Таганрог", "#АфишаТаганрог"])
    return list(dict.fromkeys(tags))

# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =====================

def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    event_type = html.escape(event.event_type.strip())
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    prices = html.escape(event.prices.strip())

    lines = [
        "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
        f"<i>{event_type}</i>",
        f"<b>{title}</b>",
        ""
    ]

    if date_str:
        lines.append(f"Дата: {date_str}")
    if time_str:
        lines.append(f"Время: {time_str}")
    if prices:
        lines.append(f"Стоимость билета: {prices}")

    # Блок записи и телефонов для мастер-классов и событий с бронью
    if event.requires_booking or event.event_type == "Мастер-класс":
        lines.append("<i><b>Предварительная запись обязательна!</b></i>")
        if event.phones:
            phone_strs = [f'<a href="tel:{link}">{formatted}</a>' for formatted, link in event.phones]
            lines.append(f"Телефон для записи: {', '.join(phone_strs)}")
    elif event.phones:
        phone_strs = [f'<a href="tel:{link}">{formatted}</a>' for formatted, link in event.phones]
        lines.append(f"Справки по телефону: {', '.join(phone_strs)}")

    lines.append("")
    if location:
        lines.append(f"<b>{location}</b>")
    if address:
        lines.append(f"Адрес: {address}")

    lines.append("")
    if event.tags:
        lines.append(" ".join(event.tags))

    return "\n".join(lines)

# ===================== ПАРСИНГ =====================

async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str, card_title: str = "") -> dict:
    data = {
        "event_type": "", "date_str": "", "parsed_date": None, "time_str": "",
        "location": "", "address": "", "prices": "", "requires_booking": False,
        "phones": [], "branch_tag": "", "buy_ticket_url": "", "image_url": None, "is_shop": False
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                
                soup = BeautifulSoup(html_text, "html.parser")
                data["image_url"] = extract_image_url(soup)
                
                full_text = soup.get_text(separator=" ")
                data["event_type"] = detect_event_type(soup, card_title, full_text)
                data["requires_booking"] = check_requires_booking(full_text)
                data["phones"] = extract_targeted_phones(full_text)

                # Определение филиала
                for branch in MUSEUM_BRANCHES:
                    if any(key in full_text.lower() for key in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break
                        
                # Поиск кнопки покупки билета
                buy_btn = soup.select_one("a[href*='vmuzey.com'], a[href*='tickets']")
                if buy_btn and buy_btn.get("href"):
                    data["buy_ticket_url"] = buy_btn["href"]

    except Exception as e:
        logger.error(f"Ошибка при парсинге детализации {detail_url}: {e}")
    return data

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                items = soup.select(".news-item, .calendar-item, .event-card")
                
                for item in items:
                    title_elem = item.select_one(".news-title, .event-title, h3, h4")
                    link_elem = item.select_one("a")
                    if not title_elem or not link_elem:
                        continue
                        
                    title = title_elem.get_text(strip=True)
                    detail_url = urljoin(base_url, link_elem.get("href", ""))
                    event_id = urlparse(detail_url).path.strip("/").replace("/", "_")
                    
                    date_elem = item.select_one(".news-date, .event-date, .date")
                    date_str = date_elem.get_text(strip=True) if date_elem else ""
                    parsed_d = parse_event_date(date_str)
                    
                    # Загрузка деталей
                    detail_data = await parse_tgliamz_detail(session, detail_url, title)
                    if detail_data.get("is_shop"):
                        continue
                        
                    tags = generate_museum_tags(title, detail_data["branch_tag"])
                    
                    event = Event(
                        event_id=event_id,
                        category=Category.MUSEUM,
                        title=title,
                        event_type=detail_data["event_type"],
                        date_str=date_str,
                        parsed_date=parsed_d,
                        time_str="",
                        location=detail_data["location"],
                        address=detail_data["address"],
                        prices="",
                        requires_booking=detail_data["requires_booking"],
                        phones=detail_data["phones"],
                        tickets_url=detail_url,
                        buy_ticket_url=detail_data["buy_ticket_url"],
                        image_url=detail_data["image_url"],
                        tags=tags
                    )
                    events.append(event)
    except Exception as e:
        logger.error(f"Ошибка при сборе списка событий: {e}")
        
    return events

async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[io.BytesIO]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://tgliamz.ru/"
    }
    try:
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 2000:
                    return io.BytesIO(data)
    except Exception as e:
        logger.warning(f"Ошибка скачивания фото {url}: {e}")
    return None

# ===================== ОСНОВНОЙ ЦИКЛ ОТПРАВКИ =====================

async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_USER_ID") or os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")
    
    if not bot_token or not channel_id:
        logger.error("Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_USER_ID/TELEGRAM_CHANNEL_ID!")
        return

    bot = Bot(token=bot_token)
    db = Database("data/taganrog_events.db")

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await parse_tgliamz_museums(session)
        
        # Хронологическая сортировка: от ближайшей даты к дальним
        events.sort(key=lambda x: (x.parsed_date or date.max, x.time_str))
        
        for event in events:
            if db.is_sent(event.event_id):
                logger.info(f"Событие {event.event_id} уже отправлялось, пропускаем.")
                continue

            caption = format_caption(event)
            reply_markup = None
            if event.buy_ticket_url:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)]
                ])

            sent_success = False
            if event.image_url:
                img_stream = await download_image(session, event.image_url)
                if img_stream:
                    try:
                        await bot.send_photo(
                            chat_id=channel_id,
                            photo=InputFile(img_stream, filename="photo.jpg"),
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                        sent_success = True
                    except TelegramError as e:
                        logger.error(f"Ошибка отправки фото для {event.event_id}: {e}")

            if not sent_success:
                try:
                    await bot.send_message(
                        chat_id=channel_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True
                    )
                    sent_success = True
                except TelegramError as e:
                    logger.error(f"Ошибка отправки сообщения для {event.event_id}: {e}")

            if sent_success:
                db.mark_as_sent(event.event_id)
                await asyncio.sleep(2)  # Пауза между отправками

if __name__ == "__main__":
    asyncio.run(main())
