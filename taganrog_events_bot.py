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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ===================== НАСТРОЙКА ЛОГИРОВАНИЯ =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== КОНСТАНТЫ И ПРАВИЛА =====================
EXCLUDED_PHONE_DIGITS = "8634610013"  # Общий справочный номер музея для исключения
GENERAL_VMUZEY_ROOT = "vmuzey.com"

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

@dataclass
class Event:
    event_id: str
    title: str
    event_type: str
    date_str: str
    parsed_date: Optional[date]
    time_str: str
    location: str
    address: str
    prices: str
    requires_booking: bool
    phones: List[str]
    hashtags: List[str]
    buy_ticket_url: str
    image_url: Optional[str]
    detail_url: str
    is_shop: bool = False

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            event_id TEXT PRIMARY KEY,
            title TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(event_id: str) -> bool:
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_event_sent(event_id: str, title: str):
    conn = sqlite3.connect("data/taganrog_events.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO sent_events (event_id, title) VALUES (?, ?)",
        (event_id, title)
    )
    conn.commit()
    conn.close()

def parse_russian_date(date_text: str) -> Optional[date]:
    clean_text = date_text.lower().strip()
    match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", clean_text)
    if not match:
        return None
    day = int(match.group(1))
    month_str = match.group(2)
    year = int(match.group(3)) if match.group(3) else datetime.now().year
    month = MONTH_MAP.get(month_str)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None

def clean_phone_number(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if EXCLUDED_PHONE_DIGITS in digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    elif len(digits) == 10:
        return f"+7 ({digits[0:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    return phone.strip()

def extract_phones(text: str) -> List[str]:
    raw_phones = re.findall(
        r"(?:(?:\+?7|8)\D{0,2})?\(?\d{3,4}\)?\D{0,2}\d{2,3}\D{0,2}\d{2}\D{0,2}\d{2}",
        text
    )
    result = []
    for p in raw_phones:
        cleaned = clean_phone_number(p)
        if cleaned and cleaned not in result:
            digits_only = re.sub(r"\D", "", cleaned)
            if EXCLUDED_PHONE_DIGITS not in digits_only:
                result.append(cleaned)
    return result

async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str, card_title: str = "") -> dict:
    data = {
        "event_type": "Литературно-музыкальная программа",
        "date_str": "",
        "parsed_date": None,
        "time_str": "10:00 - 18:00",
        "location": "Таганрогский музей-заповедник",
        "address": "ул. Октябрьская, 9",
        "prices": "В кассе музея",
        "requires_booking": False,
        "phones": [],
        "hashtags": ["#ТГЛИАМЗ", "#Таганрог", "#афиша"],
        "buy_ticket_url": "",
        "image_url": None,
        "is_shop": False
    }
    
    try:
        async with session.get(detail_url, timeout=15) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                full_text = soup.get_text()

                text_lower = full_text.lower()
                if any(w in text_lower for w in ["сувенирная", "сувениры", "музейный магазин"]):
                    data["is_shop"] = True
                    return data

                if "мастер-класс" in text_lower or "мастер класс" in text_lower or "заняти" in text_lower:
                    data["event_type"] = "Мастер-класс"
                elif "выставк" in text_lower:
                    data["event_type"] = "Выставка"
                elif "лекци" in text_lower:
                    data["event_type"] = "Публичная лекция"
                elif "концерт" in text_lower or "музык" in text_lower:
                    data["event_type"] = "Литературно-музыкальная программа"

                if "предварительная запись обязательна" in text_lower or "запись" in text_lower:
                    data["requires_booking"] = True

                extracted_p = extract_phones(full_text)
                if extracted_p:
                    data["phones"] = extracted_p

                # Дата и время
                date_match = re.search(r"(\d{1,2}\s+[а-яё]+\s*\d{4}|\d{1,2}\s+[а-яё]+)", full_text, re.I)
                if date_match:
                    d_str = date_match.group(1)
                    data["date_str"] = d_str
                    data["parsed_date"] = parse_russian_date(d_str)

                time_match = re.search(r"(\d{1,2}[:ро]\d{2}\s*[-–]\s*\d{1,2}[:ро]\d{2}|\d{1,2}\s*часов|\d{1,2}[:ро]\d{2})", full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace("ро", "00")

                # Стоимость
                price_match = re.search(r"(стоимость билета[^\.\n]+|билет[^\.\n]+руб[^\\n]*)", full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1).strip()

                # Ссылка на покупку билета
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    link_text = a.get_text().lower()
                    if "купить" in link_text or "vmuzey" in href:
                        absolute_url = urljoin(detail_url, href)
                        parsed_u = urlparse(absolute_url)
                        if GENERAL_VMUZEY_ROOT not in parsed_u.netloc or "event" in parsed_u.path:
                            data["buy_ticket_url"] = absolute_url
                            break

                # Картинка
                img_tag = soup.find("img", class_=re.compile("detail|preview|img|foto", re.I))
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin(detail_url, img_tag["src"])
                else:
                    first_img = soup.find("img")
                    if first_img and first_img.get("src"):
                        data["image_url"] = urljoin(detail_url, first_img["src"])

                # Хештеги со страницы
                tags = [tag.get_text() for tag in soup.find_all("a", href=re.compile("tags")) if tag.get_text().startswith("#")]
                if tags:
                    data["hashtags"] = tags

    except Exception as e:
        logger.error(f"Ошибка парсинга деталей {detail_url}: {e}")

    return data

async def parse_tgliamz_museums() -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"Не удалось загрузить календарь, статус: {resp.status}")
                    return []
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")

                # Жесткое отсечение дайджеста «Афиша выходного дня»
                items = soup.find_all("div", class_=re.compile(r"calendar-item|news-item|event-item|item", re.I))
                seen_urls = set()

                for item in items:
                    title_tag = item.find(["a", "h3", "h2", "div"], class_=re.compile("title|name", re.I)) or item.find("a")
                    if not title_tag:
                        continue
                    
                    title = title_tag.get_text().strip()
                    if not title or len(title) < 3:
                        continue

                    # Пропуск Афиши выходного дня и дубликатов заголовков
                    if "афиша выходного дня" in title.lower():
                        continue

                    link_tag = item.find("a", href=True)
                    if not link_tag:
                        continue

                    detail_url = urljoin(base_url, link_tag["href"])
                    
                    # Защита от дублей ссылок внутри страницы
                    parsed_path = urlparse(detail_url).path
                    if parsed_path in seen_urls or "ELEMENT_ID=3471" in detail_url:
                        continue
                    seen_urls.add(parsed_path)

                    event_id = parsed_path + "_" + urlparse(detail_url).query

                    # Парсим детальную страницу
                    detail_data = await parse_tgliamz_detail(session, detail_url, title)

                    if detail_data["is_shop"]:
                        continue

                    # Фильтрация прошедших событий
                    if detail_data["parsed_date"] and detail_data["parsed_date"] < date.today():
                        continue

                    event = Event(
                        event_id=event_id,
                        title=title,
                        event_type=detail_data["event_type"],
                        date_str=detail_data["date_str"] or "Уточняется",
                        parsed_date=detail_data["parsed_date"],
                        time_str=detail_data["time_str"],
                        location="Литературный музей А.П.Чехова",
                        address="ул. Октябрьская, 9",
                        prices=detail_data["prices"],
                        requires_booking=detail_data["requires_booking"],
                        phones=detail_data["phones"],
                        hashtags=detail_data["hashtags"],
                        buy_ticket_url=detail_data["buy_ticket_url"],
                        image_url=detail_data["image_url"],
                        detail_url=detail_url
                    )
                    events.append(event)

        except Exception as e:
            logger.error(f"Ошибка при парсинге календаря: {e}")

    return events

async def main():
    init_db()
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    user_id = os.environ.get("TELEGRAM_USER_ID")
    
    if not token or not user_id:
        logger.error("Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_USER_ID")
        return

    bot = Bot(token=token)
    events = await parse_tgliamz_museums()

    if not events:
        logger.info("Новых актуальных событий для отправки не найдено.")
        return

    # Хронологическая сортировка от ближайших к поздним
    events.sort(key=lambda x: (x.parsed_date if x.parsed_date else date.max, x.time_str))

    for event in events:
        if is_event_sent(event.event_id):
            logger.info(f"Событие уже отправлено ранее: {event.title}")
            continue

        # Формирование поста строго по шаблону (без эмодзи)
        lines = [
            "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
            f"*{event.event_type}*",
            f"*{event.title}*",
            f"Дата: {event.date_str}",
            f"Время: {event.time_str}",
            f"Стоимость билета: {event.prices}"
        ]

        if event.requires_booking:
            lines.append("*Предварительная запись обязательна!*")
            if event.phones:
                phones_str = ", ".join(event.phones)
                lines.append(f"Телефон для записи и справок: {phones_str}")

        lines.append(f"Место проведения: {event.location}, {event.address}")
        
        if event.hashtags:
            lines.append(" ".join(event.hashtags))

        caption = "\n".join(lines)

        reply_markup = None
        if event.buy_ticket_url:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)]
            ])

        try:
            if event.image_url:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=event.image_url,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
            
            mark_event_sent(event.event_id, event.title)
            logger.info(f"Успешно отправлено событие: {event.title}")
            await asyncio.sleep(1)

        except TelegramError as te:
            logger.error(f"Ошибка Telegram при отправке {event.title}: {te}")
        except Exception as ex:
            logger.error(f"Ошибка отправки {event.title}: {ex}")

if __name__ == "__main__":
    asyncio.run(main())
