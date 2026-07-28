import asyncio
import logging
import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
import schedule
import time
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import Enum

from telegram import Bot, ParseMode, InputMediaPhoto
from telegram.error import TelegramError

# ==========================================
# НАСТРОЙКИ (ЗАМЕНИ НА СВОИ)
# ==========================================
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
YOUR_CHAT_ID = "YOUR_CHAT_ID"  # или @username канала
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_errors.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_FILE = "data/taganrog_events.db"
BANNERS_DIR = "banners"

# ===================== МОДЕЛИ =====================
class Category(Enum):
    THEATRE_MONTH = "theatre_month"
    THEATRE_TODAY = "theatre_today"
    CINEMA = "cinema"
    MUSEUM = "museum"
    EVENTS = "events"
    GREENWICH = "greenwich"
    AQUALAZUR = "aqualazur"
    GOLDEN_HORSE = "golden_horse"

@dataclass
class Event:
    category: Category
    title: str
    date_str: str
    time_str: str
    location: str
    address: str
    description: str
    prices: str
    performers: str
    tickets_url: str
    phone: str
    availability: str
    age_limit: str
    duration: str
    image_url: str = ""  # URL картинки события
    fallback_banner: str = ""  # Локальный файл-заглушка

    def get_unique_key(self):
        raw = f"{self.title}_{self.date_str}_{self.time_str}_{self.tickets_url}"
        return re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ]', '_', raw)[:200]

    def get_image_source(self) -> str | None:
        """Возвращает либо URL, либо путь к локальному файлу, либо None"""
        if self.image_url and self.image_url.startswith("http"):
            return self.image_url
        if self.fallback_banner and os.path.exists(self.fallback_banner):
            return self.fallback_banner
        return None

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    os.makedirs("data", exist_ok=True)
    os.makedirs(BANNERS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_key TEXT UNIQUE,
            sent_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(event_key: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM sent_events WHERE unique_key = ?", (event_key,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_event_sent(event_key: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO sent_events (unique_key, sent_date) VALUES (?, ?)",
                       (event_key, datetime.now().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

# ===================== ИНСТРУМЕНТЫ ПАРСИНГА =====================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error(f"Ошибка загрузки {url}: {e}")
        return None

def extract_image(card: BeautifulSoup, base_url: str = "") -> str:
    """Пытается найти URL картинки в карточке события"""
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src:
            if src.startswith("//"):
                return "https:" + src
            if src.startswith("/"):
                # Пытаемся склеить с базовым URL
                from urllib.parse import urljoin
                return urljoin(base_url, src)
            if src.startswith("http"):
                return src
    # Ищем background-image в style
    style_tag = card.select_one("[style*='background-image']")
    if style_tag:
        style = style_tag.get("style", "")
        match = re.search(r"url\(['\"]?([^'\"]+)['\"]?\)", style)
        if match:
            return match.group(1)
    return ""

# ===================== ПАРСЕРЫ =====================

# 1. Театр — афиша на месяц
def parse_theatre_month() -> List[Event]:
    events = []
    url = "https://www.chehovsky.ru/repertoire/"
    soup = fetch_soup(url)
    if not soup:
        return events

    # TODO: заменить селекторы после инспекции сайта
    cards = soup.select(".repertoire-item, .event-card, article")
    for card in cards:
        title_tag = card.select_one(".title, h2, h3, a")
        date_tag = card.select_one(".date, .event-date, time")
        link_tag = card.select_one("a[href]")

        if title_tag:
            title = title_tag.get_text(strip=True)
            events.append(Event(
                category=Category.THEATRE_MONTH,
                title=title,
                date_str=date_tag.get_text(strip=True) if date_tag else "",
                time_str="19:00",
                location="Театр драмы им. Чехова",
                address="ул. Петровская, 90",
                description="",
                prices="",
                performers="",
                tickets_url=link_tag.get("href") if link_tag else url,
                phone="+7 (8634) 38-29-68",
                availability="",
                age_limit="",
                duration="",
                image_url=extract_image(card, url),
                fallback_banner=os.path.join(BANNERS_DIR, "theatre.jpg")
            ))
    return events

# 2. Театр — сегодня
def parse_theatre_today() -> List[Event]:
    events = []
    url = "https://www.chehovsky.ru/afishateatra/"
    soup = fetch_soup(url)
    if not soup:
        return events

    today_str = date.today().strftime("%d.%m.%Y")
    cards = soup.select(".afisha-item, .event, .post")
    for card in cards:
        date_tag = card.select_one(".date, .event-date, time")
        if date_tag and today_str in date_tag.get_text():
            title_tag = card.select_one(".title, h2, h3")
            link_tag = card.select_one("a[href]")
            if title_tag:
                events.append(Event(
                    category=Category.THEATRE_TODAY,
                    title=title_tag.get_text(strip=True),
                    date_str=today_str,
                    time_str="19:00",
                    location="Театр им. Чехова",
                    address="ул. Петровская, 90",
                    description="",
                    prices="",
                    performers="",
                    tickets_url=link_tag.get("href") if link_tag else url,
                    phone="+7 (8634) 38-29-68",
                    availability="",
                    age_limit="",
                    duration="",
                    image_url=extract_image(card, url),
                    fallback_banner=os.path.join(BANNERS_DIR, "theatre.jpg")
                ))
    return events

# 3. Кинотеатр Чарли
def parse_cinema() -> List[Event]:
    events = []
    url = "https://kinocharly.ru/51"
    soup = fetch_soup(url)
    if not soup:
        return events

    today_str = date.today().strftime("%d.%m")
    # TODO: заменить селекторы
    film_blocks = soup.select(".film-item, .movie-card, .schedule-item")
    for block in film_blocks:
        title_tag = block.select_one(".film-title, .movie-title, h3")
        time_tags = block.select(".session-time, .time")
        link_tag = block.select_one("a[href]")

        if title_tag:
            title = title_tag.get_text(strip=True)
            times = [t.get_text(strip=True) for t in time_tags] if time_tags else []
            events.append(Event(
                category=Category.CINEMA,
                title=title,
                date_str=today_str,
                time_str=", ".join(times) if times else "Уточняйте на сайте",
                location="Кинотеатр Чарли",
                address="ул. Дзержинского, 156",
                description="",
                prices="320 ₽ / VIP 450 ₽",
                performers="",
                tickets_url=link_tag.get("href") if link_tag else url,
                phone="",
                availability="",
                age_limit="",
                duration="",
                image_url=extract_image(block, url),
                fallback_banner=os.path.join(BANNERS_DIR, "cinema.jpg")
            ))
    return events

# 4. Музеи
def parse_museums() -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    soup = fetch_soup(url)
    if not soup:
        return events

    today = date.today()
    today_str = today.strftime("%d.%m.%Y")

    cards = soup.select(".calendar-event, .event-item, .exhibit")
    for card in cards:
        title_tag = card.select_one(".event-title, h3, .title")
        link_tag = card.select_one("a[href]")
        if title_tag:
            events.append(Event(
                category=Category.MUSEUM,
                title=title_tag.get_text(strip=True),
                date_str=today_str,
                time_str="10:00 – 18:00",
                location="Таганрогский музей-заповедник",
                address="ул. Октябрьская, 9",
                description="",
                prices="350 ₽ / студенты 175 ₽",
                performers="",
                tickets_url=link_tag.get("href") if link_tag else url,
                phone="+7 (8634) 61-02-15",
                availability="",
                age_limit="",
                duration="",
                image_url=extract_image(card, url),
                fallback_banner=os.path.join(BANNERS_DIR, "museum.jpg")
            ))
    return events

# 5. Мероприятия (GorodZovet)
def parse_events() -> List[Event]:
    events = []
    urls = [
        "https://gorodzovet.ru/taganrog/concert/",
        "https://gorodzovet.ru/taganrog/free/",
        "https://gorodzovet.ru/taganrog/lifestyle/",
        "https://gorodzovet.ru/taganrog/music/"
    ]
    today_str = date.today().strftime("%d.%m.%Y")

    for url in urls:
        soup = fetch_soup(url)
        if not soup:
            continue

        cards = soup.select(".event-card, .item, article")
        for card in cards:
            title_tag = card.select_one(".title, h3, .name")
            date_tag = card.select_one(".date, .event-date, time")
            link_tag = card.select_one("a[href]")

            # Фильтруем только сегодняшние
            if date_tag and today_str not in date_tag.get_text():
                continue

            if title_tag:
                title = title_tag.get_text(strip=True)
                href = link_tag.get("href") if link_tag else ""
                if href and not href.startswith("http"):
                    href = "https://gorodzovet.ru" + href

                events.append(Event(
                    category=Category.EVENTS,
                    title=title,
                    date_str=today_str,
                    time_str="",
                    location="",
                    address="",
                    description="",
                    prices="",
                    performers="",
                    tickets_url=href or url,
                    phone="",
                    availability="",
                    age_limit="",
                    duration="",
                    image_url=extract_image(card, url),
                    fallback_banner=os.path.join(BANNERS_DIR, "events.jpg")
                ))
    return events

# 6. Кассир.ру — дополнительный источник мероприятий
def parse_kassir() -> List[Event]:
    events = []
    url = "https://rnd.kassir.ru/prigorody/taganrog"
    soup = fetch_soup(url)
    if not soup:
        return events

    today_str = date.today().strftime("%d.%m.%Y")

    cards = soup.select(".event-item, .ticket-card, .card")
    for card in cards:
        title_tag = card.select_one(".title, .name, h3")
        date_tag = card.select_one(".date, .session-date, time")
        link_tag = card.select_one("a[href]")

        if date_tag and today_str not in date_tag.get_text():
            continue

        if title_tag:
            title = title_tag.get_text(strip=True)
            href = link_tag.get("href") if link_tag else ""
            if href and not href.startswith("http"):
                href = "https://rnd.kassir.ru" + href

            events.append(Event(
                category=Category.EVENTS,
                title=title,
                date_str=today_str,
                time_str="",
                location="",
                address="",
                description="",
                prices="",
                performers="",
                tickets_url=href or url,
                phone="",
                availability="",
                age_limit="",
                duration="",
                image_url=extract_image(card, url),
                fallback_banner=os.path.join(BANNERS_DIR, "events.jpg")
            ))
    return events

# 7. Гринвич Парк (статичный блок)
def parse_greenwich() -> List[Event]:
    return [Event(
        category=Category.GREENWICH,
        title="Гринвич Парк — SPA-комплекс",
        date_str=f"Сегодня {date.today().strftime('%d.%m.%Y')}",
        time_str="10:00 – 23:00",
        location="Гринвич Парк",
        address="ул. Адмирала Крюйса, 2а",
        description="3 бассейна, баня, хаммам, финская сауна, гидромассаж, SPA-процедуры, ресторан с видом на море",
        prices="1ч — 500 ₽ | 3ч — 1200 ₽ | 6ч — 1800 ₽ | День — 2200 ₽",
        performers="",
        tickets_url="https://greenwich-park.ru/",
        phone="+7 (863) 555-88-77",
        availability="",
        age_limit="",
        duration="",
        image_url="",
        fallback_banner=os.path.join(BANNERS_DIR, "greenwich.jpg")
    )]

# 8. Аквапарк (статичный блок)
def parse_aqualazur() -> List[Event]:
    return [Event(
        category=Category.AQUALAZUR,
        title="Аквапарк «Лазурный»",
        date_str=f"Сегодня {date.today().strftime('%d.%m.%Y')}",
        time_str="11:00 – 20:00",
        location="Аквапарк Лазурный",
        address="Парк культуры, центральная зона",
        description="15 горок, волновой бассейн, детская зона, ленивая река, фуд-корт",
        prices="3ч — 520 ₽ | 6ч — 650 ₽ | День — 750 ₽ | Семья (2+2) — 1800 ₽",
        performers="",
        tickets_url="https://akvalazur.ru/",
        phone="+7 (863) 445-12-39",
        availability="",
        age_limit="",
        duration="",
        image_url="",
        fallback_banner=os.path.join(BANNERS_DIR, "aqualazur.jpg")
    )]

# 9. Голден Хорс (статичный блок)
def parse_golden_horse() -> List[Event]:
    return [Event(
        category=Category.GOLDEN_HORSE,
        title="Загородный клуб «Голден Хорс»",
        date_str=f"Сегодня {date.today().strftime('%d.%m.%Y')}",
        time_str="12:00 – 02:00",
        location="Голден Хорс",
        address="Загородная зона (точный адрес на сайте)",
        description="Бильярд, боулинг, лазертаг, ресторан-бар, караоке, живая музыка",
        prices="Вход свободный | Бильярд 200 ₽/ч | Лазертаг 800 ₽/чел",
        performers="",
        tickets_url="https://goldenhorse161.ru/",
        phone="+7 (863) 555-44-77",
        availability="",
        age_limit="",
        duration="",
        image_url="",
        fallback_banner=os.path.join(BANNERS_DIR, "golden_horse.jpg")
    )]

# ===================== ФОРМАТТЕРЫ =====================
def format_caption(event: Event) -> str:
    """Возвращает текст подписи под фото"""
    lines = []

    if event.category == Category.THEATRE_MONTH:
        lines.append(f"🎭 <b>АФИША ТЕАТРА НА МЕСЯЦ</b>")
        lines.append(f"\n<b>{event.title}</b>")
        lines.append(f"📅 {event.date_str}")
        lines.append(f"🕐 {event.time_str}")
        lines.append(f"📍 {event.location}")
        lines.append(f"🔗 {event.tickets_url}")
        lines.append(f"📞 {event.phone}")
        lines.append(f"\n#Таганрог #театр #афиша")

    elif event.category == Category.THEATRE_TODAY:
        lines.append(f"🎭 <b>ТЕАТР СЕГОДНЯ</b>")
        lines.append(f"\n<b>{event.title}</b>")
        lines.append(f"📅 {event.date_str} | 🕐 {event.time_str}")
        lines.append(f"📍 {event.address}")
        if event.prices:
            lines.append(f"💰 {event.prices}")
        if event.availability:
            lines.append(f"⚠️ {event.availability}")
        lines.append(f"\n🔗 {event.tickets_url}")
        lines.append(f"📞 {event.phone}")
        lines.append(f"\n#Таганрог #театр #спектакль")

    elif event.category == Category.CINEMA:
        lines.append(f"🎬 <b>КИНОТЕАТР ЧАРЛИ</b>")
        lines.append(f"\n🎥 <b>{event.title}</b>")
        lines.append(f"📅 {event.date_str}")
        lines.append(f"🕐 Сеансы: {event.time_str}")
        lines.append(f"📍 {event.address}")
        lines.append(f"💰 {event.prices}")
        lines.append(f"\n🔗 {event.tickets_url}")
        lines.append(f"\n#Таганрог #кино #чарли")

    elif event.category == Category.MUSEUM:
        lines.append(f"🎨 <b>МУЗЕИ И ВЫСТАВКИ</b>")
        lines.append(f"\n🖼️ <b>{event.title}</b>")
        lines.append(f"📅 {event.date_str} | 🕐 {event.time_str}")
        lines.append(f"📍 {event.address}")
        lines.append(f"💰 {event.prices}")
        lines.append(f"\n🔗 {event.tickets_url}")
        lines.append(f"📞 {event.phone}")
        lines.append(f"\n#Таганрог #музей #выставка")

    elif event.category == Category.EVENTS:
        lines.append(f"🎪 <b>СОБЫТИЯ И КОНЦЕРТЫ</b>")
        lines.append(f"\n<b>{event.title}</b>")
        lines.append(f"📅 {event.date_str}")
        if event.location:
            lines.append(f"📍 {event.location}")
        if event.prices:
            lines.append(f"💰 {event.prices}")
        lines.append(f"\n🔗 {event.tickets_url}")
        lines.append(f"\n#Таганрог #афиша #концерт")

    elif event.category in (Category.GREENWICH, Category.AQUALAZUR, Category.GOLDEN_HORSE):
        emoji = "🌊" if event.category == Category.GREENWICH else "🎢" if event.category == Category.AQUALAZUR else "🐴"
        lines.append(f"{emoji} <b>{event.title.upper()}</b>")
        lines.append(f"\n📅 {event.date_str}")
        lines.append(f"🕐 {event.time_str}")
        lines.append(f"📍 {event.address}")
        lines.append(f"\n📝 {event.description}")
        lines.append(f"\n💰 {event.prices}")
        lines.append(f"\n🔗 {event.tickets_url}")
        lines.append(f"📞 {event.phone}")
        lines.append(f"\n#Таганрог #отдых #развлечения")

    return "\n".join(lines)

# ===================== ОТПРАВКА =====================
async def send_event(event: Event):
    """Отправляет одно событие: фото + подпись"""
    bot = Bot(token=BOT_TOKEN)
    image_source = event.get_image_source()

    try:
        if image_source:
            await bot.send_photo(
                chat_id=YOUR_CHAT_ID,
                photo=image_source,
                caption=format_caption(event),
                parse_mode=ParseMode.HTML
            )
        else:
            # Если вообще нет картинки — отправляем только текст
            await bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=format_caption(event),
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Ошибка отправки '{event.title}': {e}")
        # Fallback: пробуем отправить только текст
        try:
            await bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=format_caption(event),
                parse_mode=ParseMode.HTML
            )
        except Exception as e2:
            logger.error(f"Повторная ошибка: {e2}")

# ===================== ГЛАВНАЯ ЛОГИКА =====================
def run_async(coro):
    """Костыль для запуска async из синхронного кода"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(coro)
    loop.close()
    return result

def process_and_send():
    logger.info("=" * 40)
    logger.info("Запуск сбора данных...")
    init_db()

    today = date.today()
    is_monday = today.weekday() == 0

    parsers = [
        ("theatre_month", parse_theatre_month, is_monday),
        ("theatre_today", parse_theatre_today, True),
        ("cinema", parse_cinema, True),
        ("museums", parse_museums, True),
        ("events", parse_events, True),
        ("kassir", parse_kassir, True),
        ("greenwich", parse_greenwich, True),
        ("aqualazur", parse_aqualazur, True),
        ("golden_horse", parse_golden_horse, True),
    ]

    total_sent = 0

    for name, parser_func, is_active in parsers:
        if not is_active:
            logger.info(f"Блок {name}: пропущен (не активен)")
            continue

        try:
            events = parser_func()
            sent_count = 0

            for event in events:
                key = event.get_unique_key()
                if not is_event_sent(key):
                    run_async(send_event(event))
                    mark_event_sent(key)
                    sent_count += 1
                    time.sleep(1.5)  # Щадим API Telegram

            logger.info(f"Блок {name}: {len(events)} событий, отправлено новых: {sent_count}")
            total_sent += sent_count

        except Exception as e:
            logger.error(f"Ошибка парсера {name}: {e}", exc_info=True)

    logger.info(f"Всего отправлено новых событий: {total_sent}")
    logger.info("=" * 40)

def main():
    logger.info("Бот запущен.")
    init_db()

    # Для немедленного теста — раскомментируй следующую строку:
    # process_and_send()

    schedule.every().day.at("09:00").do(process_and_send)
    logger.info("Расписание: каждый день в 09:00 МСК")

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
