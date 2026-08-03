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

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {"keys": ["литературный музей", "литературно-музыкальн", "чехова, 29"], "name": "Литературный музей А.П. Чехова", "tag": "#МузейЧехова"},
    {"keys": ["домик чехова", "чехова, 69"], "name": "Мемориальный музей «Домик А.П. Чехова»", "tag": "#ДомикЧехова"},
    {"keys": ["лавка чеховых", "александровская, 100"], "name": "Музей «Лавка Чеховых»", "tag": "#ЛавкаЧеховых"},
    {"keys": ["дворец алфераки", "историко-краеведческ", "фрунзе, 41"], "name": "Историко-краеведческий музей (Дворец Алфераки)", "tag": "#ДворецАлфераки"},
    {"keys": ["градостроительства", "фрунзе, 80"], "name": "Музей градостроительства и быта", "tag": "#МузейГрадостроительства"},
    {"keys": ["художественный музей", "александровская, 56"], "name": "Таганрогский художественный музей", "tag": "#ХудожественныйМузей"},
    {"keys": ["самбекские высоты"], "name": "Самбекские высоты", "tag": "#СамбекскиеВысоты"},
    {"keys": ["дурова", "пер. глушко, 44"], "name": "Музей Анатолия Дурова", "tag": "#МузейДурова"},
]


@dataclass
class Event:
    event_id: str
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
    buy_ticket_url: str = ""
    image_url: Optional[str] = None
    tags: List[str] = field(default_factory=list)


class EventDatabase:
    def __init__(self, db_path: str = "data/events.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.create_table()

    def create_table(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def is_sent(self, event_id: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM sent_events WHERE event_id = ?", (event_id,))
        return cursor.fetchone() is not None

    def mark_sent(self, event_id: str):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO sent_events (event_id) VALUES (?)", (event_id,))


def parse_russian_date(date_text: str) -> Optional[date]:
    """Извлекает дату из текстового формата (например, '18 августа 2024 г.')"""
    try:
        match = re.search(r"(\d{1,2})\s+([а-яяА-Я]+)(?:\s+(\d{4}))?", date_text.lower())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            year = int(match.group(3)) if match.group(3) else date.today().year
            
            for m_name, m_num in MONTH_MAP.items():
                if m_name in month_str:
                    return date(year, m_num, day)
    except Exception as e:
        logger.warning(f"Ошибка парсинга даты '{date_text}': {e}")
    return None


def is_souvenir_shop_item(html_text: str) -> bool:
    text_lower = html_text.lower()
    return any(word in text_lower for word in STRICT_SOUVENIR_WORDS)


def match_branch(text: str):
    text_lower = text.lower()
    for branch in MUSEUM_BRANCHES:
        if any(key in text_lower for key in branch["keys"]):
            return branch["name"], branch["tag"]
    return "Музеи Таганрога", "#ТГЛИАМЗ"


async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str, card_title: str = "") -> dict:
    data = {
        "event_type": "", "date_str": "", "parsed_date": None, "time_str": "",
        "location": "", "address": "", "prices": "", "requires_booking": False,
        "phones": [], "buy_ticket_url": "", "image_url": None, "is_shop": False, "branch_tag": ""
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                
                soup = BeautifulSoup(html_text, "html.parser")

                # Картинка
                img_tag = soup.find("img", class_=re.compile(r"detail|photo|main", re.I)) or soup.find("img")
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin(detail_url, img_tag["src"])

                page_text = soup.get_text(separator="\n")

                # Тип мероприятия
                if "мастер-класс" in page_text.lower():
                    data["event_type"] = "Мастер-класс"
                elif "экскурсия" in page_text.lower():
                    data["event_type"] = "Экскурсия"
                elif "выставка" in page_text.lower():
                    data["event_type"] = "Выставка"
                elif "программа" in page_text.lower() or "концерт" in page_text.lower():
                    data["event_type"] = "Литературно-музыкальная программа"

                # Даты
                date_match = re.search(r"(\d{1,2}\s+[а-яА-Я]+(?:\s+\d{4})?\s*(?:г\.)?)", page_text)
                if date_match:
                    data["date_str"] = date_match.group(1).strip()
                    data["parsed_date"] = parse_russian_date(data["date_str"])

                # Время
                time_match = re.search(r"(\d{1,2}[:.-]\d{2})", page_text)
                if time_match:
                    data["time_str"] = time_match.group(1).replace("-", ":").replace(".", ":")

                # Стоимость
                price_match = re.search(r"(\d+\s*руб[а-я\.]*(?:\s*\([^)]+\))?)", page_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1).strip()

                # Запись и Телефоны
                if "запись" in page_text.lower() or "предварительн" in page_text.lower():
                    data["requires_booking"] = True

                phones = re.findall(r"(\+?7|8)[\s\(-]*\d{3,4}[\s\)-]*\d{2,3}[\s-]*\d{2}[\s-]*\d{2}", page_text)
                if phones:
                    data["phones"] = list(set([re.sub(r"[^\d+]", "", p) for p in phones]))

                # Ссылка на билеты
                ticket_btn = soup.find("a", href=re.compile(r"vmuzey|kassa|ticket", re.I))
                if ticket_btn and ticket_btn.get("href"):
                    data["buy_ticket_url"] = ticket_btn["href"]

                # Локация
                loc_name, loc_tag = match_branch(page_text + " " + card_title)
                data["location"] = loc_name
                data["branch_tag"] = loc_tag

    except Exception as e:
        logger.error(f"Ошибка парсинга детальной страницы {detail_url}: {e}")

    return data


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    base_url = "https://tgliamz.ru"
    calendar_url = "https://tgliamz.ru/calendar/"

    try:
        async with session.get(calendar_url, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                cards = soup.find_all("div", class_=re.compile(r"item|card|event", re.I))

                for card in cards:
                    link_tag = card.find("a", href=True)
                    if not link_tag:
                        continue
                    
                    detail_url = urljoin(base_url, link_tag["href"])
                    title = card.get_text(strip=True)
                    
                    detail_data = await parse_tgliamz_detail(session, detail_url, card_title=title)
                    if detail_data.get("is_shop"):
                        continue

                    event_id = urlparse(detail_url).path.strip("/").replace("/", "_")
                    
                    tags = ["#Таганрог", "#АфишаТаганрог"]
                    if detail_data.get("branch_tag"):
                        tags.insert(0, detail_data["branch_tag"])

                    event = Event(
                        event_id=event_id,
                        title=title if len(title) < 80 else title[:77] + "...",
                        url=detail_url,
                        event_type=detail_data.get("event_type", ""),
                        date_str=detail_data.get("date_str", ""),
                        parsed_date=detail_data.get("parsed_date"),
                        time_str=detail_data.get("time_str", ""),
                        location=detail_data.get("location", ""),
                        address=detail_data.get("address", ""),
                        prices=detail_data.get("prices", ""),
                        requires_booking=detail_data.get("requires_booking", False),
                        phones=detail_data.get("phones", []),
                        buy_ticket_url=detail_data.get("buy_ticket_url", ""),
                        image_url=detail_data.get("image_url"),
                        tags=tags
                    )
                    events.append(event)
    except Exception as e:
        logger.error(f"Ошибка при сборе афиши ТГЛИАМЗ: {e}")

    return events


def format_caption(event: Event) -> str:
    """
    Формирует текст поста с аккуратными переносами строк в чистом строгом стиле.
    """
    blocks = []

    # 1. Шапка источника
    blocks.append("МУЗЕЙНАЯ АФИША ТАГАНРОГА")

    # 2. Тип и название мероприятия
    title_block = []
    if event.event_type:
        title_block.append(f"<i>{html.escape(event.event_type.strip())}</i>")
    if event.title:
        title_block.append(f"{html.escape(event.title.strip())}")
    if title_block:
        blocks.append("\n".join(title_block))

    # 3. Дата, время и стоимость
    info_block = []
    if event.date_str:
        info_block.append(f"Дата: {html.escape(event.date_str.strip())}")
    if event.time_str:
        info_block.append(f"Время: {html.escape(event.time_str.strip())}")
    if event.prices:
        info_block.append(f"Стоимость билета: {html.escape(event.prices.strip())}")
    if info_block:
        blocks.append("\n".join(info_block))

    # 4. Предварительная запись и телефоны (жирный + курсив)
    booking_block = []
    if event.requires_booking or event.event_type == "Мастер-класс":
        booking_block.append("<b><i>Предварительная запись обязательна!</i></b>")
        if event.phones:
            phone_list = [p[0] if isinstance(p, tuple) else str(p) for p in event.phones]
            booking_block.append(f"Телефон для записи: {', '.join(phone_list)}")
    elif event.phones:
        phone_list = [p[0] if isinstance(p, tuple) else str(p) for p in event.phones]
        booking_block.append(f"Контакты: {', '.join(phone_list)}")
    if booking_block:
        blocks.append("\n".join(booking_block))

    # 5. Площадка и адрес
    location_block = []
    if event.location:
        location_block.append(f"{html.escape(event.location.strip())}")
    if event.address:
        location_block.append(f"Адрес: {html.escape(event.address.strip())}")
    if location_block:
        blocks.append("\n".join(location_block))

    # 6. Хештеги
    if event.tags:
        blocks.append(" ".join(event.tags))

    return "\n\n".join(blocks)


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[io.BytesIO]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://tgliamz.ru/"
    }
    try:
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                content = await resp.read()
                return io.BytesIO(content)
    except Exception as e:
        logger.error(f"Ошибка скачивания фото {url}: {e}")
    return None


async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error("Ошибка: Переменные окружения TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID не заданы!")
        return

    bot = Bot(token=bot_token)
    db = EventDatabase()

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        all_events = await parse_tgliamz_museums(session)

        # 1. Фильтруем прошедшие даты
        valid_events = [
            e for e in all_events
            if e.parsed_date is None or e.parsed_date >= date.today()
        ]

        # 2. ХРОНОЛОГИЧЕСКАЯ СОРТИРОВКА (от ближней даты к дальней)
        valid_events.sort(key=lambda x: (x.parsed_date or date.max, x.time_str))

        logger.info(f"Найдено событий для обработки: {len(valid_events)}")

        for event in valid_events:
            if db.is_sent(event.event_id):
                continue

            caption = format_caption(event)

            reply_markup = None
            if event.buy_ticket_url:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)]
                ])

            try:
                # Отправка фото с подписью (картинка автоматически наверху)
                if event.image_url:
                    photo_bytes = await download_image(session, event.image_url)
                    if photo_bytes:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=InputFile(photo_bytes, filename="event.jpg"),
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                    else:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )

                db.mark_sent(event.event_id)
                logger.info(f"Успешно отправлено событие: {event.title}")
                await asyncio.sleep(2)  # Небольшая пауза между отправками

            except TelegramError as e:
                logger.error(f"Ошибка отправки в Telegram для '{event.title}': {e}")


if __name__ == "__main__":
    asyncio.run(main())
