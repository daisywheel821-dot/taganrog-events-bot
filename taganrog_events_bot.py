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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError

# НАСТРОЙКА ЛОГИРОВАНИЯ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция",
    "купить сувенир",
    "в продаже сувениры",
    "музейный магазин",
    "прейскурант цен на товары",
    "каталог сувениров"
]

EXCLUDED_PHONE_DIGITS = "8634610013"
GLOBAL_BUY_TICKET_BASE = "vmuzey.com"

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн", "юрнкц", "чехова"],
        "name": "Литературный музей А.П. Чехова",
        "address": "ул. Октябрьская, 9"
    },
    {
        "keys": ["историко-краеведческий", "алфераки", "краеведческий"],
        "name": "Историко-краеведческий музей (Дворец Алфераки)",
        "address": "ул. Фрунзе, 41"
    },
    {
        "keys": ["градостроительства", "быта"],
        "name": "Музей «Градостроительство и быт г. Таганрога»",
        "address": "ул. Фрунзе, 80"
    },
    {
        "keys": ["фаина раневская", "раневской"],
        "name": "Выставочный зал «Дом Фаины Раневской»",
        "address": "ул. Фрунзе, 10"
    },
]

@dataclass
class Event:
    element_id: str
    title: str
    detail_url: str
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
    is_shop: bool = False
    hashtags: List[str] = field(default_factory=list)

def init_db(db_path: str = "data/taganrog_events.db"):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            element_id TEXT PRIMARY KEY,
            title TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(element_id: str, db_path: str = "data/taganrog_events.db") -> bool:
    if not element_id:
        return False
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_events WHERE element_id = ?", (element_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_event_sent(element_id: str, title: str, db_path: str = "data/taganrog_events.db"):
    if not element_id:
        return
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO sent_events (element_id, title) VALUES (?, ?)
    """, (element_id, title))
    conn.commit()
    conn.close()

def is_souvenir_shop_item(html_text: str) -> bool:
    text_lower = html_text.lower()
    for phrase in STRICT_SOUVENIR_WORDS:
        if phrase in text_lower:
            return True
    return False

def clean_phone_number(phone_raw: str) -> Optional[str]:
    digits = re.sub(r'\D', '', phone_raw)
    if not digits:
        return None
    if EXCLUDED_PHONE_DIGITS in digits:
        return None
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 11 and digits.startswith('7'):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    elif len(digits) == 10:
        return f"+7 ({digits[0:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    elif len(digits) == 6:
        return f"8 (8634) {digits[0:2]}-{digits[2:4]}-{digits[4:]}"
    return phone_raw.strip()

def parse_russian_date(date_text: str) -> Optional[date]:
    text = date_text.lower().strip()
    match = re.search(r'(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?', text)
    if not match:
        return None
    day = int(match.group(1))
    month_name = match.group(2)
    year = int(match.group(3)) if match.group(3) else datetime.now().year
    month = MONTH_MAP.get(month_name)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None

def determine_branch(text: str) -> tuple[str, str]:
    text_lower = text.lower()
    for branch in MUSEUM_BRANCHES:
        for key in branch["keys"]:
            if key in text_lower:
                return branch["name"], branch["address"]
    return "Таганрогский музей-заповедник", "г. Таганрог"

async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str, card_title: str = "") -> dict:
    data = {
        "event_type": "",
        "date_str": "",
        "parsed_date": None,
        "time_str": "",
        "location": "",
        "address": "",
        "prices": "",
        "requires_booking": False,
        "phones": [],
        "branch_tag": "",
        "buy_ticket_url": "",
        "image_url": None,
        "is_shop": False,
        "hashtags": []
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                
                soup = BeautifulSoup(html_text, "lxml")
                
                img_tag = soup.select_one(".detail_picture, .news-detail-image img, img.preview_picture, .item-image img, article img")
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin(detail_url, img_tag["src"])
                
                text_content = soup.get_text()
                title_lower = card_title.lower()
                
                if "мастер-класс" in title_lower or "мастер класс" in title_lower:
                    data["event_type"] = "Мастер-класс"
                elif "лекция" in title_lower:
                    data["event_type"] = "Публичная лекция"
                elif "выставка" in title_lower:
                    data["event_type"] = "Выставка"
                elif "концерт" in title_lower or "джаз" in title_lower or "программа" in title_lower:
                    data["event_type"] = "Литературно-музыкальная программа"
                else:
                    data["event_type"] = "Музейное мероприятие"

                date_match = re.search(r'(\d{1,2}\s+[а-яё]+\s*(?:\d{4})?)', text_content, re.I)
                if date_match:
                    d_str = date_match.group(1)
                    data["date_str"] = d_str
                    data["parsed_date"] = parse_russian_date(d_str)

                time_match = re.search(r'(?:в\s+)?(\d{1,2}[:.]\d{2})', text_content)
                if time_match:
                    data["time_str"] = time_match.group(1).replace('.', ':')

                loc_name, loc_addr = determine_branch(text_content + " " + card_title)
                data["location"] = loc_name
                data["address"] = loc_addr

                price_match = re.search(r'(стоимость\s+билета[:\s]*[^\.\n]+)', text_content, re.I)
                if price_match:
                    data["prices"] = price_match.group(1).strip()
                else:
                    data["prices"] = "В кассе музея"

                if "предварительная запись обязательна" in text_content.lower() or "запись" in text_content.lower():
                    data["requires_booking"] = True

                phones_found = re.findall(r'(?:\+7|8)[\s\-]?\(?\d{3,5}\)?[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2}', text_content)
                cleaned_phones = []
                for p in phones_found:
                    cp = clean_phone_number(p)
                    if cp and cp not in cleaned_phones:
                        cleaned_phones.append(cp)
                data["phones"] = cleaned_phones

                ticket_a = soup.find("a", href=re.compile(r'vmuzey\.com', re.I))
                if ticket_a and ticket_a.get("href"):
                    link = ticket_a["href"]
                    if GLOBAL_BUY_TICKET_BASE not in link or len(link) > len("https://vmuzey.com/"):
                        data["buy_ticket_url"] = link

                tag_elems = soup.find_all(string=re.compile(r'#\w+'))
                hashtags = []
                for te in tag_elems:
                    found_tags = re.findall(r'#([А-Яа-яЁё\w]+)', te)
                    for ft in found_tags:
                        tag_str = f"#{ft}"
                        if tag_str not in hashtags:
                            hashtags.append(tag_str)
                if not hashtags:
                    hashtags = ["#ТГЛИАМЗ", "#Таганрог", "#афиша"]
                data["hashtags"] = hashtags

    except Exception as e:
        logger.error(f"Ошибка при парсинге детали страницы {detail_url}: {e}")
    return data

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://tgliamz.ru/"
    }

    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                logger.error(f"Не удалось загрузить календарь: статус {resp.status}")
                return events
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "lxml")

            items = soup.find_all("div", class_=re.compile(r'item|calendar|event|news', re.I))
            seen_ids = set()

            for item in items:
                link_tag = item.find("a", href=True)
                if not link_tag:
                    continue
                href = link_tag["href"]
                if "ELEMENT_ID=" not in href:
                    continue
                
                element_id_match = re.search(r'ELEMENT_ID=(\d+)', href)
                if not element_id_match:
                    continue
                element_id = element_id_match.group(1)

                if element_id == "3471" or "афиша выходного дня" in item.get_text().lower()[:50]:
                    continue

                if element_id in seen_ids:
                    continue
                seen_ids.add(element_id)

                title_tag = item.find(["h2", "h3", "h4", "a"])
                title = title_tag.get_text(strip=True) if title_tag else "Мероприятие"
                if not title or len(title) < 3:
                    title = "Событие музея"

                detail_url = urljoin(base_url, href)

                detail_data = await parse_tgliamz_detail(session, detail_url, title)
                if detail_data["is_shop"]:
                    continue

                event_date = detail_data["parsed_date"]
                if event_date and event_date < date.today():
                    continue

                event = Event(
                    element_id=element_id,
                    title=title,
                    detail_url=detail_url,
                    event_type=detail_data["event_type"],
                    date_str=detail_data["date_str"],
                    parsed_date=event_date,
                    time_str=detail_data["time_str"],
                    location=detail_data["location"],
                    address=detail_data["address"],
                    prices=detail_data["prices"],
                    requires_booking=detail_data["requires_booking"],
                    phones=detail_data["phones"],
                    branch_tag=detail_data["branch_tag"],
                    buy_ticket_url=detail_data["buy_ticket_url"],
                    image_url=detail_data["image_url"],
                    hashtags=detail_data["hashtags"]
                )
                events.append(event)

    except Exception as e:
        logger.error(f"Ошибка при парсинге календаря TGLIAMZ: {e}")

    return events

async def download_image_bytes(session: aiohttp.ClientSession, image_url: str) -> Optional[io.BytesIO]:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(image_url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.read()
                return io.BytesIO(data)
    except Exception as e:
        logger.error(f"Не удалось скачать картинку {image_url}: {e}")
    return None

def format_event_post(event: Event) -> str:
    lines = [
        "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
        f"_{event.event_type}_",
        f"*{event.title}*",
        f"Дата: {event.date_str}" if event.date_str else "",
        f"Время: {event.time_str}" if event.time_str else "",
        f"Стоимость билета: {event.prices}" if event.prices else "",
        f"Место: {event.location}, {event.address}" if event.location else ""
    ]
    
    lines = [l for l in lines if l]

    if event.requires_booking:
        lines.append("")
        lines.append("*Предварительная запись обязательна!*")
        if event.phones:
            phones_str = ", ".join(event.phones)
            lines.append(f"Телефоны для справок: {phones_str}")

    if event.hashtags:
        lines.append("")
        lines.append(" ".join(event.hashtags))

    return "\n".join(lines)

async def main():
    init_db()

    bot_token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_USER_ID")

    if not bot_token or not chat_id:
        logger.error("ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены!")
        return

    bot = Bot(token=bot_token)

    async with aiohttp.ClientSession() as session:
        events = await parse_tgliamz_museums(session)
        logger.info(f"Найдено событий для проверки: {len(events)}")

        events.sort(key=lambda x: (x.parsed_date if x.parsed_date else date.max))

        sent_count = 0
        for event in events:
            if is_event_sent(event.element_id):
                logger.info(f"Событие уже отправлено ранее (пропускаем): {event.title} (ID: {event.element_id})")
                continue

            post_text = format_event_post(event)
            
            reply_markup = None
            if event.buy_ticket_url:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)]
                ])

            try:
                photo_bio = None
                if event.image_url:
                    photo_bio = await download_image_bytes(session, event.image_url)

                if photo_bio:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=InputFile(photo_bio, filename="event.jpg"),
                        caption=post_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=post_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup
                    )

                mark_event_sent(event.element_id, event.title)
                sent_count += 1
                logger.info(f"Успешно отправлено: {event.title}")
                await asyncio.sleep(1)

            except TelegramError as te:
                logger.error(f"Ошибка Telegram при отправке события {event.title}: {te}")
            except Exception as e:
                logger.error(f"Общая ошибка при отправке события {event.title}: {e}")

        logger.info(f"Готово! Всего отправлено новых постов: {sent_count}")

if __name__ == "__main__":
    asyncio.run(main())
