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
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
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
            cursor = conn.cursor()
            cursor.execute("""
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
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO sent_events (event_id) VALUES (?)", (event_id,))
            conn.commit()


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_souvenir_shop_item(text: str) -> bool:
    check_str = text.lower()
    return any(word in check_str for word in STRICT_SOUVENIR_WORDS)


def parse_event_date(date_text: str) -> Optional[date]:
    if not date_text:
        return None
    
    text_lower = date_text.lower()
    match = re.search(r"(\d{1,2})\s+([а-я]+)", text_lower)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)
        month = MONTH_MAP.get(month_str)
        if month:
            today = date.today()
            year = today.year
            if month < today.month and (today.month - month) > 6:
                year += 1
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def extract_image_url(soup: BeautifulSoup, base_url: str = "https://tgliamz.ru") -> Optional[str]:
    """Извлекает прямую ссылку на фото новости из блока .news-item-img."""
    img_tag = soup.select_one(".news-item-img img")
    if img_tag and img_tag.get("src"):
        return urljoin(base_url, img_tag["src"])
    
    link_tag = soup.select_one(".news-item-img a[href]")
    if link_tag and link_tag.get("href"):
        return urljoin(base_url, link_tag["href"])
        
    return None


def extract_targeted_phones(text_block: str) -> List[tuple]:
    """Извлекает целевые телефоны из текста, отсекая общий номер музея."""
    phone_pattern = r"(?:\+?7|8)?[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}|\b\d{2}[\s\-]?\d{2}[\s\-]?\d{2}\b"

    match = re.search(r"(?:телефон|тел\.?|справки|бронирование)[^:\n]*[:\s]+([^\n<]+)", text_block, re.IGNORECASE)
    target_chunk = match.group(1) if match else text_block

    raw_phones = re.findall(phone_pattern, target_chunk)
    result = []
    seen = set()

    for raw in raw_phones:
        digits = re.sub(r"\D", "", raw)
        
        if EXCLUDED_PHONE_DIGITS in digits or len(digits) < 6:
            continue

        if len(digits) == 6:
            display = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
            tel = f"+78634{digits}"
        elif len(digits) in (10, 11):
            if digits.startswith("8"):
                digits = "7" + digits[1:]
            elif len(digits) == 10:
                digits = "7" + digits

            if len(digits) == 11 and digits[1] == '9':
                display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
            else:
                display = f"8 ({digits[1:5]}) {digits[5:7]}-{digits[7:9]}-{digits[9:]}"
            tel = f"+{digits}"
        else:
            continue

        if tel not in seen:
            seen.add(tel)
            result.append((display, tel))

    return result


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
    
    return "Музейная программа"


def check_requires_booking(text: str) -> bool:
    clean_text = " ".join(text.lower().split())
    keywords = [
        "предварительная запись обязательна",
        "предварительная запись",
        "запись по телефону",
        "бронирование мест обязательно",
        "бронирование мест",
        "обязательна предварительная запись",
        "по предварительной записи",
        "по предварительной брони"
    ]
    return any(kw in clean_text for kw in keywords)


def generate_museum_tags(text: str, branch_tag: str) -> List[str]:
    tags = ["#ТГЛИАМЗ"]
    if branch_tag:
        tags.append(branch_tag)

    text_lower = text.lower()
    if "джаз" in text_lower or "концерт" in text_lower or "музык" in text_lower:
        tags.append("#музыкавмузее")
    if "мастер-класс" in text_lower or "мастер класс" in text_lower:
        tags.append("#мастеркласс")
    if "выставк" in text_lower or "экспозиц" in text_lower:
        tags.append("#выставка")
    
    tags.append("#программы")
    tags.extend(["#Таганрог", "#афиша"])

    unique_tags = []
    for t in tags:
        if t not in unique_tags:
            unique_tags.append(t)
    return unique_tags


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    event_type = html.escape(event.event_type.strip())
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    prices = html.escape(event.prices.strip())

    lines = []

    # 1. Шапка
    if event.category == Category.THEATRE_MONTH:
        lines.append("<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
        lines.append("<i>Репертуар и анонс спектаклей</i>\n")
    elif event.category == Category.MUSEUM:
        lines.append("<b>МУЗЕЙНАЯ АФИША ТАГАНРОГА</b>")
        if event_type:
            lines.append(f"<i>{event_type}</i>\n")
        else:
            lines.append("<i>Музейная программа</i>\n")

    # 2. Название
    lines.append(f"<b>«{title}»</b>\n")

    # 3. Детали
    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")
    
    if prices:
        if event.buy_ticket_url:
            lines.append(f"<b>Стоимость билета:</b> {prices} (онлайн / в кассе музея)")
        else:
            lines.append(f"<b>Стоимость билета:</b> {prices} (в кассе музея)")

    if event.requires_booking:
        lines.append("<b>Предварительная запись обязательна!</b>")

    # 4. Локация и адрес
    if location:
        lines.append(f"\n{location}")
    if address:
        lines.append(f"{address}.")

    # 5. Телефоны
    if event.phones:
        lines.append("\n📞 <b>Справки и запись по телефону:</b>")
        for disp, tel in event.phones:
            lines.append(f"<a href='tel:{tel}'>{disp}</a>")

    # 6. Хэштеги
    if event.tags:
        lines.append("\n" + " ".join(event.tags))
    else:
        lines.append("\n#Таганрог #афиша")

    return "\n".join(lines)


# ===================== ПАРСИНГ =====================
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
        "is_shop": False
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data

                soup = BeautifulSoup(html_text, "html.parser")
                content_div = soup.select_one(".news-item-text") or soup
                page_full_text = content_div.get_text()

                # 1. Поиск картинки
                data["image_url"] = extract_image_url(soup)

                # 2. Поиск ссылки на покупку билета
                buy_link = soup.select_one("a[href*='vmuzey.com/event/']")
                if buy_link:
                    data["buy_ticket_url"] = buy_link["href"].strip()

                # 3. Извлечение телефонов (без общемузейного)
                data["phones"] = extract_targeted_phones(page_full_text)

                # 4. Проверка на запись и определение типа
                data["event_type"] = detect_event_type(soup, card_title, page_full_text)
                data["requires_booking"] = check_requires_booking(page_full_text)

                # 5. Локация и филиал
                for branch in MUSEUM_BRANCHES:
                    if any(k in page_full_text.lower() for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # 6. Извлечение даты
                date_match = re.search(r"(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", page_full_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1)
                    data["parsed_date"] = parse_event_date(data["date_str"])

                # 7. Извлечение времени
                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", page_full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                # 8. Извлечение цены
                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*|цена[^\d]*?)(\d+\s*руб[а-я]*[^\.\n,]*)", page_full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1).strip()

    except Exception as e:
        logger.warning(f"Ошибка парсинга детальной страницы {detail_url}: {e}")
    return data


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                html_content = await response.text()
                soup = BeautifulSoup(html_content, "html.parser")

                items = soup.select(".news-item, .event-card, .calendar-item, .item, .col-md-4, .col-sm-6, .col-xs-12, a.news-item-link")
                
                for item in items:
                    title_el = item.select_one(".title, .name, h2, h3, h4, .news-title")
                    
                    if not title_el and item.name == 'a':
                        title = item.get_text(strip=True)
                    elif title_el:
                        title = title_el.get_text(strip=True)
                    else:
                        continue

                    title = title.strip(" «»\"'")

                    if len(title) < 3 or "подробнее" in title.lower():
                        continue

                    link_el = item if item.name == 'a' else item.select_one("a[href]")
                    if not link_el or not link_el.get("href"):
                        continue

                    tickets_url = urljoin(base_url, link_el["href"])
                    event_id = f"tgliamz_{hash(tickets_url)}"

                    detail_data = await parse_tgliamz_detail(session, tickets_url, card_title=title)
                    if detail_data["is_shop"]:
                        continue

                    final_tags = generate_museum_tags(title, detail_data["branch_tag"])

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            title=title,
                            event_type=detail_data["event_type"],
                            date_str=detail_data["date_str"],
                            parsed_date=detail_data["parsed_date"],
                            time_str=detail_data["time_str"],
                            location=detail_data["location"] or "Таганрогский музей-заповедник",
                            address=detail_data["address"],
                            prices=detail_data["prices"],
                            requires_booking=detail_data["requires_booking"],
                            phones=detail_data["phones"],
                            tickets_url=tickets_url,
                            buy_ticket_url=detail_data["buy_ticket_url"],
                            image_url=detail_data["image_url"],
                            tags=final_tags
                        )
                    )
    except Exception as e:
        logger.error(f"Ошибка парсинга Музеев (ТГЛИАМЗ): {e}")

    unique_events = {}
    for ev in events:
        if ev.event_id not in unique_events:
            unique_events[ev.event_id] = ev

    return list(unique_events.values())


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[io.BytesIO]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены!")
        return

    bot = Bot(token=bot_token)
    db = Database()
    today = date.today()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        logger.info("Запуск Парсинга...")
        events = await parse_tgliamz_museums(session)
        logger.info(f"Всего найдено мероприятий: {len(events)}")

        for event in events:
            if event.parsed_date and event.parsed_date < today:
                logger.info(f"Событие [{event.title}] от {event.parsed_date} уже прошло, пропускаем.")
                continue

            if db.is_sent(event.event_id):
                logger.info(f"Событие [{event.title}] уже было отправлено, пропускаем.")
                continue

            caption = format_caption(event)
            photo_sent = False

            reply_markup = None
            if event.buy_ticket_url:
                keyboard = [[InlineKeyboardButton("🎫 Купить билет", url=event.buy_ticket_url)]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            if event.image_url:
                img_stream = await download_image(session, event.image_url)
                if img_stream:
                    try:
                        input_photo = InputFile(img_stream, filename="photo.jpg")
                        await bot.send_photo(
                            chat_id=channel_id,
                            photo=input_photo,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                        photo_sent = True
                    except Exception as e:
                        logger.warning(f"Ошибка отправки фото [{event.title}]: {e}")

            if not photo_sent:
                try:
                    await bot.send_message(
                        chat_id=channel_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup
                    )
                except TelegramError as e:
                    logger.error(f"Ошибка отправки сообщения [{event.title}]: {e}")
                    continue

            db.mark_as_sent(event.event_id)
            logger.info(f"Успешно отправлено в Telegram: {event.title}")
            await asyncio.sleep(2)

    logger.info("Запуск завершен.")

if __name__ == "__main__":
    asyncio.run(main())
