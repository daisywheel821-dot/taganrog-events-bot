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
logger = logging.getLogger("taganrog_events_bot")

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция",
    "купить сувенир",
    "в продаже сувениры",
    "музейный магазин",
    "прейскурант цен на товары",
    "каталог сувениров"
]

# Глобальная общая ссылка музея, которую нужно игнорировать как индивидуальную
GLOBAL_TICKET_URL = "https://vmuzey.com/museum/taganrogskiy-muzey-zapovednik"

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн"],
        "tag": "#ЮРНКЦЧехова",
        "name": "Литературный музей А.П. Чехова"
    },
    {
        "keys": ["домик чехова"],
        "tag": "#ДомикЧехова",
        "name": "Домик Чехова"
    },
    {
        "keys": ["лавка чеховых"],
        "tag": "#ЛавкаЧеховых",
        "name": "Лавка чеховых"
    },
    {
        "keys": ["историко-краеведческий", "алфераки"],
        "tag": "#ДворецАлфераки",
        "name": "Историко-краеведческий музей (Дворец Алфераки)"
    },
    {
        "keys": ["градостроительств", "быт"],
        "tag": "#МузейГрадостроительства",
        "name": "Музей градостроительства и быта"
    },
    {
        "keys": ["выставочный зал"],
        "tag": "#ВыставочныйЗалТГЛИАМЗ",
        "name": "Выставочный зал ТГЛИАМЗ"
    }
]

@dataclass
class Event:
    title: str
    detail_url: str
    event_type: str
    date_str: str
    parsed_date: Optional[date]
    time_str: str
    location: str
    address: str
    prices: str
    requires_booking: bool
    phones: List[str]
    branch_tag: str
    buy_ticket_url: str
    image_url: Optional[str]
    image_bytes: Optional[io.BytesIO] = field(default=None, repr=False)
    hashtags: List[str] = field(default_factory=list)

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            url TEXT PRIMARY KEY,
            title TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(url: str) -> bool:
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_events WHERE url = ?", (url,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_event_sent(url: str, title: str):
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO sent_events (url, title) VALUES (?, ?)", (url, title))
    conn.commit()
    conn.close()

def is_souvenir_shop_item(html_text: str) -> bool:
    text_lower = html_text.lower()
    return any(word in text_lower for word in STRICT_SOUVENIR_WORDS)

def format_phone(phone_digits: str) -> str:
    """Приводит сырые цифры к красивому читаемому виду"""
    clean = re.sub(r'\D', '', phone_digits)
    if len(clean) == 11 and clean.startswith(('7', '8')):
        return f"+7 ({clean[1:4]}) {clean[4:7]}-{clean[7:9]}-{clean[9:]}"
    elif len(clean) == 6:
        return f"+7 (8634) {clean[:2]}-{clean[2:4]}-{clean[4:]}"
    return phone_digits

def extract_targeted_phones(html_text: str) -> List[str]:
    phones = []
    # Ищем стандартные паттерны телефонов в тексте
    found = re.findall(r'(?:[\d\(\)\-\s]{7,15})', html_text)
    for f in found:
        digits = re.sub(r'\D', '', f)
        if 6 <= len(digits) <= 11:
            formatted = format_phone(f.strip())
            if formatted not in phones:
                phones.append(formatted)
    return phones

def parse_date_string(date_text: str) -> Optional[date]:
    date_text = date_text.lower().strip()
    current_year = datetime.now().year
    
    match = re.search(r'(\d{1,2})\s+([а-яё]+)', date_text)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)
        month = MONTH_MAP.get(month_str)
        if month:
            try:
                return date(current_year, month, day)
            except ValueError:
                pass
    return None

def determine_branch(text: str) -> tuple:
    text_lower = text.lower()
    for branch in MUSEUM_BRANCHES:
        for key in branch["keys"]:
            if key in text_lower:
                return branch["tag"], branch["name"]
    return "#ТГЛИАМЗ", "Таганрогский музей-заповедник"

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
        async with session.get(detail_url, timeout=15) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                    
                soup = BeautifulSoup(html_text, "html.parser")
                full_text = soup.get_text()
                
                # Ищем индивидуальные хештеги на странице
                found_hashtags = re.findall(r'#[\wА-Яа-яЁё]+', full_text)
                unique_tags = []
                for tag in found_hashtags:
                    if tag not in unique_tags:
                        unique_tags.append(tag)
                data["hashtags"] = unique_tags if unique_tags else ["#ТГЛИАМЗ", "#Таганрог", "#АфишаТаганрог"]

                # Поиск времени
                time_match = re.search(r'(\d{1,2}[:\.]\d{2})', full_text)
                if time_match:
                    data["time_str"] = time_match.group(1).replace('.', ':')

                # Поиск телефонов
                data["phones"] = extract_targeted_phones(full_text)
                
                if "предварительн" in full_text.lower() or "запись" in full_text.lower() or data["phones"]:
                    data["requires_booking"] = True

                # Поиск ссылки на билеты с исключением общей ссылки
                buy_btn = soup.find("a", href=re.compile(r'vmuzey|afisha|tickets', re.I))
                if buy_btn and buy_btn.get('href'):
                    candidate_url = urljoin(detail_url, buy_btn['href'])
                    if GLOBAL_TICKET_URL not in candidate_url:
                        data["buy_ticket_url"] = candidate_url

                # Определение филиала
                branch_tag, branch_name = determine_branch(full_text + " " + card_title)
                data["branch_tag"] = branch_tag
                data["location"] = branch_name
                
                # Поиск картинки события
                img_elem = soup.find("img", class_=re.compile(r'detail|main|preview|photo', re.I)) or soup.find("article").find("img") if soup.find("article") else None
                if img_elem and img_elem.get('src'):
                    data["image_url"] = urljoin(detail_url, img_elem['src'])

    except Exception as e:
        logger.error(f"Ошибка парсинга детальной страницы {detail_url}: {e}")
        
    return data

async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[io.BytesIO]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://tgliamz.ru/"
    }
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.read()
                return io.BytesIO(data)
    except Exception as e:
        logger.error(f"Ошибка скачивания картинки {url}: {e}")
    return None

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    seen_urls = set()  # Защита от дублей в рамках одного запуска
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                logger.error(f"Ошибка загрузки календаря: {resp.status}")
                return events
            html_content = await resp.text()
            
        soup = BeautifulSoup(html_content, "html.parser")
        items = soup.find_all("div", class_=re.compile(r'event|calendar-item|afisha|news-item', re.I))
        
        for item in items:
            title_elem = item.find(['h3', 'h4', 'a'], class_=re.compile(r'title|name', re.I)) or item.find('a')
            if not title_elem:
                continue
                
            card_title = title_elem.get_text(strip=True)
            
            # Исключаем сводный еженедельный дайджест-баннер
            if "афиша выходного дня" in card_title.lower():
                continue
                
            link_elem = item.find('a', href=True)
            if not link_elem:
                continue
                
            detail_url = urljoin(base_url, link_elem['href'])
            
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            
            # Поиск даты в карточке
            date_elem = item.find(class_=re.compile(r'date|time|calendar', re.I))
            date_str = date_elem.get_text(strip=True) if date_elem else ""
            parsed_date = parse_date_string(date_str)
            
            # Отсеиваем прошедшие события
            if parsed_date and parsed_date < date.today():
                continue

            # Детальный парсинг
            detail_data = await parse_tgliamz_detail(session, detail_url, card_title)
            if detail_data["is_shop"]:
                continue
                
            final_date_str = date_str if date_str else detail_data["date_str"]
            final_parsed_date = parsed_date or detail_data["parsed_date"]
            
            # Картинка
            img_url = detail_data["image_url"]
            if not img_url:
                img_elem = item.find('img')
                if img_elem and img_elem.get('src'):
                    img_url = urljoin(base_url, img_elem['src'])
                    
            image_bytes = None
            if img_url:
                image_bytes = await download_image(session, img_url)

            # Типизация мероприятия
            event_type = "Мероприятие"
            title_lower = card_title.lower()
            if "мастер-класс" in title_lower or "мастер класс" in title_lower:
                event_type = "Мастер-класс"
            elif "выставк" in title_lower:
                event_type = "Выставка"
            elif "экскурси" in title_lower:
                event_type = "Экскурсия"
            elif "концерт" in title_lower or "музыкальн" in title_lower or "джаз" in title_lower:
                event_type = "Литературно-музыкальная программа"

            events.append(Event(
                title=card_title,
                detail_url=detail_url,
                event_type=event_type,
                date_str=final_date_str,
                parsed_date=final_parsed_date,
                time_str=detail_data["time_str"],
                location=detail_data["location"],
                address=detail_data["address"],
                prices=detail_data["prices"],
                requires_booking=detail_data["requires_booking"],
                phones=detail_data["phones"],
                branch_tag=detail_data["branch_tag"],
                buy_ticket_url=detail_data["buy_ticket_url"],
                image_url=img_url,
                image_bytes=image_bytes,
                hashtags=detail_data["hashtags"]
            ))
            
    except Exception as e:
        logger.error(f"Ошибка парсинга календаря музея: {e}")
        
    return events

def format_event_post(event: Event) -> str:
    lines = [
        "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
        f"*{event.event_type}*",
        f"*{event.title}*",
        ""
    ]
    
    if event.date_str:
        lines.append(f"Дата: {event.date_str}")
    if event.time_str:
        lines.append(f"Время: {event.time_str}")
    
    if event.location:
        loc_str = event.location
        if event.address:
            loc_str += f", {event.address}"
        lines.append(f"Место: {loc_str}")
        
    if event.prices:
        lines.append(f"Стоимость: {event.prices}")
        
    if event.requires_booking:
        lines.append("")
        lines.append("⚠️ Предварительная запись обязательна!")
        if event.phones:
            phones_str = ", ".join(event.phones)
            lines.append(f"Телефоны для записи: {phones_str}")
            
    if event.hashtags:
        lines.append("")
        lines.append(" ".join(event.hashtags))
    
    return "\n".join(lines)

async def send_event_to_telegram(bot: Bot, chat_id: str, event: Event):
    if is_event_sent(event.detail_url):
        return
        
    caption = format_event_post(event)
    
    reply_markup = None
    if event.buy_ticket_url:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)]
        ])
        
    try:
        if event.image_bytes:
            event.image_bytes.seek(0)
            await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(event.image_bytes, filename="event.jpg"),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
        mark_event_sent(event.detail_url, event.title)
        logger.info(f"Успешно отправлено: {event.title}")
    except TelegramError as e:
        logger.error(f"Ошибка отправки в Telegram для {event.title}: {e}")

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    user_id = os.getenv("TELEGRAM_USER_ID")
    
    if not token or not user_id:
        logger.error("Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_USER_ID в переменных окружения!")
        return

    init_db()
    bot = Bot(token=token)
    
    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий с сайта ТГЛИАМЗ...")
        events = await parse_tgliamz_museums(session)
        
        # Сортировка по дате (ближайшие первыми)
        events.sort(key=lambda x: x.parsed_date if x.parsed_date else date.max)
        
        logger.info(f"Найдено актуальных событий для отправки: {len(events)}")
        
        for event in events:
            await send_event_to_telegram(bot, user_id, event)
            await asyncio.sleep(1) # Пауза между сообщениями

if __name__ == "__main__":
    asyncio.run(main())
