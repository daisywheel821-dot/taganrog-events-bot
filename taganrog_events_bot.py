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

# Цифры общего телефона, который нужно полностью исключать
EXCLUDED_PHONE_DIGITS = ["8634383496", "383496"]

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
        "keys": ["юрНКц", "южно-российский"],
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


def extract_targeted_phones(text: str) -> List[tuple]:
    """Парсит телефоны ТОЛЬКО после ключевых фраз (справки, запись и т.д.)."""
    
    # Ключевые фразы, после которых ищем номера
    trigger_patterns = [
        r"телефон[ы]?\s+для\s+(?:справок|записи)[^:\n]*[:\s]?",
        r"справки\s+по\s+телефон[уам][:\s]?",
        r"запись\s+по\s+телефон[уам][:\s]?",
        r"информация\s+по\s+телефон[уам][:\s]?",
        r"бронирование\s+билетов\s+по\s+телефон[уам][:\s]?"
    ]
    
    combined_trigger = "|".join(trigger_patterns)
    
    # Регулярное выражение для поиска фрагментов текста сразу после ключевых слов
    segments = re.split(f"({combined_trigger})", text, flags=re.IGNORECASE)
    
    phone_pattern = r"(?:\+?7|8)?[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}|\b\d{2}[\s\-]?\d{2}[\s\-]?\d{2}\b"
    
    formatted_phones = []
    seen_digits = set()

    for i in range(1, len(segments), 2):
        # Берём кусок текста сразу после найденной фразы (ограничиваем 150 символами)
        target_chunk = segments[i+1][:150] if (i+1) < len(segments) else ""
        raw_phones = re.findall(phone_pattern, target_chunk)

        for raw in raw_phones:
            digits = re.sub(r"\D", "", raw)
            if not digits:
                continue

            # Исключаем главный справочный телефон музея (38-34-96)
            if any(ex in digits for ex in EXCLUDED_PHONE_DIGITS):
                continue

            # Обработка 6-значных городских номеров (например, 61-14-66)
            if len(digits) == 6:
                if digits in seen_digits or f"8634{digits}" in seen_digits:
                    continue
                seen_digits.add(digits)
                seen_digits.add(f"8634{digits}")
                
                display = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
                tel = f"+78634{digits}"
                formatted_phones.append((display, tel))

            # Обработка 11-значных номеров (8-999-692-04-53 или 8 (8634) 61-14-66)
            elif len(digits) in (10, 11):
                if len(digits) == 10:
                    digits = "7" + digits
                elif digits.startswith("8"):
                    digits = "7" + digits[1:]

                if digits in seen_digits:
                    continue
                seen_digits.add(digits)

                # Мобильные номера (+7 9xx ...)
                if digits[1] == '9':
                    display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                    tel = f"+{digits}"
                # Городские Таганрога (+7 8634 ...)
                elif digits[1:5] == '8634':
                    display = f"8 (8634) {digits[5:7]}-{digits[7:9]}-{digits[9:]}"
                    tel = f"+{digits}"
                else:
                    display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                    tel = f"+{digits}"

                formatted_phones.append((display, tel))

    return formatted_phones


def clean_image_url(base_url: str, raw_src: str) -> Optional[str]:
    """Очищает и проверяет валидность URL картинки."""
    if not raw_src:
        return None

    if "url(" in raw_src:
        match = re.search(r"url\((['\"]?)(.*?)\1\)", raw_src)
        if match:
            raw_src = match.group(2)

    raw_src = raw_src.strip()
    if raw_src.startswith("data:") or raw_src.endswith(".svg") or raw_src.endswith(".gif"):
        return None

    full_url = urljoin(base_url, raw_src)
    path = urlparse(full_url).path.lower()
    
    if any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return full_url

    return None


def detect_event_type(soup: BeautifulSoup, title: str, full_text: str) -> str:
    selectors = [
        ".category-title", ".subtitle", ".event-type", ".news-category",
        ".section-title", ".type", ".detail-type", ".theme-title"
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 2:
            return el.get_text(strip=True)

    combined = (title + " " + full_text).lower()
    if "мастер-класс" in combined or "мастер класс" in combined:
        return "Мастер-класс"
    elif "концерт" in combined or "джаз" in combined or "литературно-музыкальн" in combined:
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
        tags.append("#концерт")
    if "мастер-класс" in text_lower or "мастер класс" in text_lower:
        tags.append("#мастеркласс")
        tags.append("#творчество")
    if "выставк" in text_lower or "экспозиц" in text_lower:
        tags.append("#выставка")
    if "программ" in text_lower or "экскурси" in text_lower or "лекци" in text_lower:
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
async def parse_chehov_theatre(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://www.chehovsky.ru/afishateatra/"
    base_url = "https://www.chehovsky.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                html_content = await response.text()
                soup = BeautifulSoup(html_content, "html.parser")

                cards = soup.select(".afisha-item, .event-item, .views-row, tr.afisha_row")
                for card in cards:
                    title_el = card.select_one(".title, .name, h3, h4, .afisha_title, .field-name-title")
                    date_el = card.select_one(".date, .afisha_date, .field-name-field-date")
                    time_el = card.select_one(".time, .afisha_time")
                    price_el = card.select_one(".price, .afisha_price")
                    img_el = card.select_one("img")
                    link_el = card.select_one("a[href]")

                    if title_el:
                        title = title_el.get_text(strip=True)
                        date_str = date_el.get_text(strip=True) if date_el else ""
                        time_str = time_el.get_text(strip=True) if time_el else ""
                        prices = price_el.get_text(strip=True) if price_el else ""

                        parsed_dt = parse_event_date(date_str)

                        tickets_url = url
                        if link_el and link_el.get("href"):
                            tickets_url = urljoin(base_url, link_el["href"])

                        image_url = None
                        if img_el:
                            src = img_el.get("src") or img_el.get("data-src")
                            image_url = clean_image_url(base_url, src)

                        event_id = f"chehov_{hash(title + date_str + tickets_url)}"

                        events.append(
                            Event(
                                event_id=event_id,
                                category=Category.THEATRE_MONTH,
                                title=title,
                                event_type="Спектакль",
                                date_str=date_str,
                                parsed_date=parsed_dt,
                                time_str=time_str,
                                location="Таганрогский театр\nим. А.П. Чехова",
                                address="ул. Петровская, 90",
                                prices=prices,
                                phones=[("8 (8634) 38-29-68", "+78634382968")],
                                tickets_url=tickets_url,
                                buy_ticket_url="",
                                image_url=image_url,
                                tags=["#Таганрог", "#ТеатрЧехова", "#афиша"]
                            )
                        )
    except Exception as e:
        logger.error(f"Ошибка парсинга Театра Чехова: {e}")

    return events


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
        "image_url": "",
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
                page_full_text = soup.get_text()

                main_content = soup.select_one(".news-detail, .detail_text, .content, .workarea") or soup

                for a_tag in main_content.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    if "vmuzey.com/event/" in href:
                        data["buy_ticket_url"] = href
                        break

                data["event_type"] = detect_event_type(soup, card_title, page_full_text)

                # Поиск фото
                for img in main_content.select("img"):
                    src = img.get("src") or img.get("data-src") or img.get("data-original")
                    cleaned_img = clean_image_url("https://tgliamz.ru", src)
                    if cleaned_img:
                        data["image_url"] = cleaned_img
                        break

                data["requires_booking"] = check_requires_booking(page_full_text)

                for branch in MUSEUM_BRANCHES:
                    if any(k in page_full_text.lower() for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # Извлекаем телефоны СТРОГО после целевых фраз
                data["phones"] = extract_targeted_phones(page_full_text)

                date_match = re.search(r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", page_full_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()
                    data["parsed_date"] = parse_event_date(data["date_str"])

                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", page_full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*|цена[^\d]*?)(\d+\s*руб[а-я]*[^\.\n]*)", page_full_text, re.I)
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

                    date_el = item.select_one(".date, .time, .calendar-date")
                    loc_el = item.select_one(".location, .place, .museum-title")
                    
                    img_el = item.select_one("img")
                    card_image_url = None
                    if img_el:
                        src = img_el.get("src") or img_el.get("data-src") or img_el.get("style")
                        card_image_url = clean_image_url(base_url, src)

                    link_el = item if item.name == 'a' else item.select_one("a[href]")

                    date_str = date_el.get_text(strip=True) if date_el else ""
                    location_card = loc_el.get_text(strip=True) if loc_el else "Таганрогский музей-заповедник"

                    tickets_url = url
                    if link_el and link_el.get("href"):
                        tickets_url = urljoin(base_url, link_el["href"])

                    event_id = f"tgliamz_{hash(tickets_url)}"

                    detail_data = await parse_tgliamz_detail(session, tickets_url, card_title=title)
                    if detail_data["is_shop"]:
                        continue

                    final_image_url = detail_data["image_url"] or card_image_url
                    final_location = detail_data["location"] or location_card
                    final_tags = generate_museum_tags(title, detail_data["branch_tag"])

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            title=title,
                            event_type=detail_data["event_type"],
                            date_str=detail_data["date_str"] or date_str,
                            parsed_date=detail_data["parsed_date"] or parse_event_date(date_str),
                            time_str=detail_data["time_str"],
                            location=final_location,
                            address=detail_data["address"],
                            prices=detail_data["prices"],
                            requires_booking=detail_data["requires_booking"],
                            phones=detail_data["phones"],
                            tickets_url=tickets_url,
                            buy_ticket_url=detail_data["buy_ticket_url"],
                            image_url=final_image_url,
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


async def fetch_events(session: aiohttp.ClientSession) -> List[Event]:
    all_events = []
    chehov_events = await parse_chehov_theatre(session)
    all_events.extend(chehov_events)

    museum_events = await parse_tgliamz_museums(session)
    all_events.extend(museum_events)

    return all_events


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[io.BytesIO]:
    """Скачивание картинки с валидацией содержимого."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
        "Referer": "https://tgliamz.ru/"
    }
    try:
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "svg" in content_type:
                    return None

                data = await resp.read()
                if len(data) > 2000:
                    file_stream = io.BytesIO(data)
                    return file_stream
    except Exception as e:
        logger.warning(f"Ошибка скачивания фото по адресу {url}: {e}")
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
        events = await fetch_events(session)
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
            if event.buy_ticket_url and "vmuzey.com/event/" in event.buy_ticket_url:
                keyboard = [[InlineKeyboardButton("🎫 Купить билет", url=event.buy_ticket_url)]]
                reply_markup = InlineKeyboardMarkup(keyboard)

            if event.image_url:
                img_stream = await download_image(session, event.image_url)
                if img_stream:
                    try:
                        # Фикс белого прямоугольника: обертка в InputFile с расширением filename
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
                        logger.warning(f"Ошибка отправки фото байтами [{event.title}]: {e}")

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
                    logger.error(f"Ошибка отправки сообщения без фото [{event.title}]: {e}")
                    continue

            db.mark_as_sent(event.event_id)
            logger.info(f"Успешно отправлено в Telegram: {event.title}")

            await asyncio.sleep(2)

    logger.info("Запуск завершен.")


if __name__ == "__main__":
    asyncio.run(main())
