import asyncio
import html
import io
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
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

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция",
    "купить сувенир",
    "в продаже сувениры",
    "музейный магазин",
    "прейскурант цен на товары",
    "каталог сувениров",
]

MONTH_MAP = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн"],
        "name": "Литературный музей\nА.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЛитературныйМузейЧехова",
    },
    {
        "keys": ["юрнкц", "южно-российский"],
        "name": "ЮРНКЦ\nА.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЮРНКЦЧехова",
    },
    {
        "keys": ["дворец алфераки", "историко-краеведческий"],
        "name": "Историко-краеведческий музей\n(Дворец Алфераки)",
        "address": "ул. Фрунзе, 41",
        "tag": "#ДворецАлфераки",
    },
    {
        "keys": ["домик чехова"],
        "name": "Музей «Домик Чехова»",
        "address": "ул. Чехова, 69",
        "tag": "#ДомикЧехова",
    },
    {
        "keys": ["лавка чеховых", "лавка чехова"],
        "name": "Музей «Лавка Чеховых»",
        "address": "ул. Александровская, 100",
        "tag": "#ЛавкаЧеховых",
    },
    {
        "keys": ["градостроительства"],
        "name": "Музей градостроительства и быта",
        "address": "ул. Фрунзе, 80",
        "tag": "#МузейГрадостроительства",
    },
    {
        "keys": ["дурова"],
        "name": "Музей А.А. Дурова",
        "address": "ул. А. Глушко, 44",
        "tag": "#МузейДурова",
    },
    {
        "keys": ["василенко"],
        "name": "Музей И.Д. Василенко",
        "address": "ул. Чехова, 88",
        "tag": "#МузейВасиленко",
    },
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
    phones: List[str] = field(default_factory=list)
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_events (
                    event_id TEXT PRIMARY KEY,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.commit()

    def is_sent(self, event_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sent_events WHERE event_id = ?", (event_id,)
            )
            return cursor.fetchone() is not None

    def mark_as_sent(self, event_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sent_events (event_id) VALUES (?)",
                (event_id,),
            )
            conn.commit()


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_souvenir_shop_item(text: str) -> bool:
    check_str = text.lower()
    return any(word in check_str for word in STRICT_SOUVENIR_WORDS)


def parse_event_date(date_text: str) -> Optional[date]:
    """Умное извлечение даты для хронологической сортировки."""
    if not date_text:
        return None

    clean_text = date_text.lower().strip()
    today = date.today()

    found_month = None
    for month_name, month_num in MONTH_MAP.items():
        if month_name in clean_text:
            found_month = month_num
            break

    if not found_month:
        return None

    numbers = re.findall(r"\b\d{1,2}\b", clean_text)
    if not numbers:
        return None

    day = int(numbers[0])

    year_match = re.search(r"\b(20\d{2})\b", clean_text)
    year = int(year_match.group(1)) if year_match else today.year

    try:
        parsed = date(year, found_month, day)
        if not year_match and (today - parsed).days > 180:
            parsed = date(year + 1, found_month, day)
        return parsed
    except ValueError:
        return None


def extract_image_url(
    soup: BeautifulSoup, base_url: str = "https://tgliamz.ru"
) -> Optional[str]:
    img_tag = soup.select_one(".news-item-img img")
    if img_tag and img_tag.get("src"):
        return urljoin(base_url, img_tag["src"])
    return None


def extract_targeted_phones(text_block: str) -> List[str]:
    """Извлекает и форматирует все телефоны из текста без исключений."""
    phone_pattern = r"(?:\+7|8)?[\s(-]*\d{3,4}[\s)-]*\d{2,3}[\s-]*\d{2}[\s-]*\d{2}|\b\d{2}[\s-]?\d{2}[\s-]?\d{2}\b"
    matches = re.findall(phone_pattern, text_block)
    cleaned_phones = []

    for raw in matches:
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("7"):
            digits = "8" + digits[1:]

        if len(digits) == 11:
            formatted = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
        elif len(digits) == 6:
            formatted = f"{digits[:2]}-{digits[2:4]}-{digits[4:6]}"
        else:
            formatted = raw.strip()

        if formatted not in cleaned_phones:
            cleaned_phones.append(formatted)

    return cleaned_phones


def detect_event_type(soup: BeautifulSoup, title: str, full_text: str) -> str:
    combined = (title + " " + full_text).lower()
    if "мастер-класс" in combined or "мастер класс" in combined:
        return "Мастер-класс"
    elif (
        "литературно-музыкальн" in combined
        or "джаз" in combined
        or "концерт" in combined
    ):
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
        "предварительная запись обязательна",
        "предварительная запись",
        "запись по телефону",
        "бронирование мест обязательно",
        "бронирование мест",
        "обязательна предварительная запись",
        "по предварительной записи",
        "по предварительной брони",
    ]
    return any(kw in clean_text for kw in keywords)


def generate_museum_tags(text: str, branch_tag: str) -> List[str]:
    tags = ["#ТГЛИАМЗ"]
    if branch_tag:
        tags.append(branch_tag)
    return tags


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    event_type = html.escape(event.event_type.strip())
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    prices = html.escape(event.prices.strip())

    lines = [f"<b>{title}</b>\n"]

    if event_type:
        lines.append(f"<b>Категория:</b> {event_type}")
    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")
    if location:
        lines.append(f"<b>Место:</b> {location}")
    if address:
        lines.append(f"<b>Адрес:</b> {address}")
    if prices:
        lines.append(f"<b>Стоимость:</b> {prices}")

    # Блок записи по телефону
    if event.event_type == "Мастер-класс" or event.requires_booking:
        lines.append("\n<b>⚠️ Предварительная запись обязательна!</b>")
        if event.phones:
            phones_formatted = ", ".join(
                [f"<code>{p}</code>" for p in event.phones]
            )
            lines.append(f"<b>Запись по телефону:</b> {phones_formatted}")
    elif event.phones:
        phones_formatted = ", ".join(
            [f"<code>{p}</code>" for p in event.phones]
        )
        lines.append(f"<b>Справки по телефону:</b> {phones_formatted}")

    if event.tags:
        lines.append(f"\n{' '.join(event.tags)}")

    return "\n".join(lines)


# ===================== ПАРСИНГ =====================
async def parse_tgliamz_detail(
    session: aiohttp.ClientSession, detail_url: str, card_title: str = ""
) -> dict:
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
    }

    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")

                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data

                full_text = soup.get_text(separator=" ", strip=True)

                data["event_type"] = detect_event_type(
                    soup, card_title, full_text
                )
                data["image_url"] = extract_image_url(soup)
                data["requires_booking"] = check_requires_booking(full_text)
                data["phones"] = extract_targeted_phones(full_text)

                for branch in MUSEUM_BRANCHES:
                    if any(
                        key in full_text.lower() for key in branch["keys"]
                    ):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                date_block = soup.select_one(
                    ".news-detail-date, .event-date, .date"
                )
                if date_block:
                    d_text = date_block.get_text(strip=True)
                    data["date_str"] = d_text
                    data["parsed_date"] = parse_event_date(d_text)

    except Exception as e:
        logger.error(f"Ошибка при парсинге детализации {detail_url}: {e}")

    return data


async def parse_tgliamz_museums(
    session: aiohttp.ClientSession,
) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"

    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                items = soup.select(".news-item, .calendar-item, .item")

                for item in items:
                    title_elem = item.select_one(".news-name, a.title, .title")
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    link = (
                        title_elem.get("href")
                        if title_elem.name == "a"
                        else item.select_one("a")["href"]
                    )
                    detail_url = urljoin(base_url, link)

                    event_id = f"tgliamz_{detail_url.split('/')[-1] or detail_url.split('/')[-2]}"

                    detail_data = await parse_tgliamz_detail(
                        session, detail_url, card_title=title
                    )

                    if detail_data["is_shop"]:
                        continue

                    date_str = detail_data["date_str"]
                    parsed_date = detail_data["parsed_date"]

                    if not parsed_date:
                        date_elem = item.select_one(".news-date, .date")
                        if date_elem:
                            date_str = date_elem.get_text(strip=True)
                            parsed_date = parse_event_date(date_str)

                    tags = generate_museum_tags(
                        title, detail_data["branch_tag"]
                    )

                    event = Event(
                        event_id=event_id,
                        category=Category.MUSEUM,
                        title=title,
                        event_type=detail_data["event_type"],
                        date_str=date_str,
                        parsed_date=parsed_date,
                        time_str=detail_data["time_str"],
                        location=detail_data["location"],
                        address=detail_data["address"],
                        prices=detail_data["prices"],
                        requires_booking=detail_data["requires_booking"],
                        phones=detail_data["phones"],
                        tickets_url=detail_url,
                        buy_ticket_url=detail_data["buy_ticket_url"],
                        image_url=detail_data["image_url"],
                        tags=tags,
                    )
                    events.append(event)
    except Exception as e:
        logger.error(f"Ошибка при парсинге списка афиши: {e}")

    return events


async def download_image(
    session: aiohttp.ClientSession, url: str
) -> Optional[io.BytesIO]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://tgliamz.ru/",
    }
    try:
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 2000:
                    bio = io.BytesIO(data)
                    bio.name = "photo.jpg"
                    return bio
    except Exception as e:
        logger.warning(f"Ошибка скачивания фото {url}: {e}")
    return None


# ===================== ОСНОВНОЙ ЦИКЛ ОТПРАВКИ =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

    if not bot_token or not channel_id:
        logger.error(
            "Не заданы переменные окружения TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID"
        )
        return

    bot = Bot(token=bot_token)
    db = Database()

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await parse_tgliamz_museums(session)

        new_events = [e for e in events if not db.is_sent(e.event_id)]

        if not new_events:
            logger.info("Новых событий не найдено.")
            return

        min_date = date.min
        new_events.sort(key=lambda e: e.parsed_date or min_date)

        logger.info(
            f"Найдено {len(new_events)} новых событий. Отправка по хронологии..."
        )

        for event in new_events:
            caption = format_caption(event)

            reply_markup = None
            if event.tickets_url:
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "Подробнее на сайте", url=event.tickets_url
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                if event.image_url:
                    img_bytes = await download_image(session, event.image_url)
                    if img_bytes:
                        await bot.send_photo(
                            chat_id=channel_id,
                            photo=img_bytes,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                    else:
                        await bot.send_message(
                            chat_id=channel_id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                else:
                    await bot.send_message(
                        chat_id=channel_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                    )

                db.mark_as_sent(event.event_id)
                logger.info(
                    f"Успешно отправлено: {event.title} ({event.parsed_date})"
                )

                await asyncio.sleep(2)

            except TelegramError as e:
                logger.error(
                    f"Ошибка Telegram при отправке '{event.title}': {e}"
                )
            except Exception as e:
                logger.error(
                    f"Непредвиденная ошибка при отправке '{event.title}': {e}"
                )


if __name__ == "__main__":
    asyncio.run(main())
