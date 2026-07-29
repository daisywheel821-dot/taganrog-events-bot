import asyncio
import logging
import os
import re
import sqlite3
import html
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ===================== НАСТРОЙКА ЛОГИРОВАНИЯ =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ===================== МОДЕЛИ ДАННЫХ =====================
class Category(Enum):
    THEATRE = "THEATRE"
    MUSEUM = "MUSEUM"


@dataclass
class Event:
    event_id: str
    category: Category
    event_type: str  # СПЕКТАКЛЬ, КОНЦЕРТ, ВЫСТАВКА, МАСТЕР-КЛАСС
    title: str
    age_limit: str = ""  # 0+, 6+, 12+, 16+, 18+
    date_str: str = ""
    time_str: str = ""
    location: str = ""
    address: str = ""
    participants: str = ""  # В программе примут участие
    description: str = ""   # Программа / Описание
    prices: str = ""
    phone: str = ""
    is_preorder_required: bool = False  # Предварительная запись обязательна
    tickets_url: str = ""
    image_url: Optional[str] = None


# ===================== РАБОТА С БАЗОЙ ДАННЫХ =====================
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


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ (БЕЗ ЭМОДЗИ) =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    event_type = html.escape(event.event_type.strip().upper())
    age = f" ({html.escape(event.age_limit.strip())})" if event.age_limit else ""
    
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    participants = html.escape(event.participants.strip())
    description = html.escape(event.description.strip())
    prices = html.escape(event.prices.strip())
    phone = html.escape(event.phone.strip())
    tickets_url = html.escape(event.tickets_url.strip())

    lines = []

    # 1. Шапка категории и тип события
    if event.category == Category.THEATRE:
        lines.append(f"<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
    else:
        lines.append(f"<b>ТАГАНРОГСКИЙ МУЗЕЙ-ЗАПОВЕДНИК</b>")
    
    lines.append(f"<b>{event_type}{age} | {title}</b>\n")

    # 2. Дата и время
    datetime_parts = []
    if date_str:
        datetime_parts.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        datetime_parts.append(f"<b>Время:</b> {time_str}")
    if datetime_parts:
        lines.append(" | ".join(datetime_parts))

    # 3. Локация и адрес
    if location and address:
        lines.append(f"<b>Место:</b> {location} ({address})")
    elif location:
        lines.append(f"<b>Место:</b> {location}")
    elif address:
        lines.append(f"<b>Адрес:</b> {address}")

    # 4. Стоимость
    if prices:
        lines.append(f"<b>Стоимость:</b> {prices}")

    # 5. Ограничение мест / Предварительная запись
    if event.is_preorder_required:
        lines.append("\n<b>Важно: Количество мест ограничено. Предварительная запись обязательна!</b>")

    # 6. Участники и исполнители
    if participants:
        lines.append(f"\n<b>В программе примут участие:</b>\n{participants}")

    # 7. Программа / Описание
    if description:
        lines.append(f"\n<b>Программа:</b>\n{description}")

    # 8. Справки и Покупка билетов
    contact_parts = []
    if phone:
        contact_parts.append(f"<b>Телефон для справок и бронирования:</b> {phone}")
    if tickets_url:
        action_text = "Записаться на мероприятие" if event.is_preorder_required else "Купить билет здесь"
        contact_parts.append(f"👉 <a href='{tickets_url}'><b>{action_text}</b></a>")

    if contact_parts:
        lines.append("\n" + "\n".join(contact_parts))

    # 9. Хэштеги
    tags = ["#Таганрог", "#АфишаТаганрог"]
    if event.category == Category.THEATRE:
        tags.extend(["#ТеатрЧехова", "#ТеатрТаганрог"])
    else:
        tags.extend(["#МузейТаганрог", "#ВыставкиТаганрог"])
    
    if "МАСТЕР-КЛАСС" in event_type:
        tags.append("#МастерКлассТаганрог")
    elif "КОНЦЕРТ" in event_type:
        tags.append("#КонцертТаганрог")

    lines.append("\n" + " ".join(tags))

    return "\n".join(lines)


# ===================== ПАРСИНГ: ТЕАТР ЧЕХОВА =====================
async def parse_chehov_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    """Глубокий парсинг внутренней страницы спектакля/концерта"""
    data = {
        "description": "",
        "participants": "",
        "age_limit": "",
        "image_url": None,
        "is_preorder": False
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")

                # Поиск фото
                img_el = soup.select_one(".performance-main-img img, .field-name-field-image img, .content img")
                if img_el and img_el.get("src"):
                    data["image_url"] = urljoin("https://www.chehovsky.ru", img_el["src"])

                # Поиск возрастного ценза
                age_el = soup.select_one(".age-limit, .age, .field-name-field-age")
                if age_el:
                    data["age_limit"] = age_el.get_text(strip=True)
                else:
                    match = re.search(r"(\d+\+)", html_text)
                    if match:
                        data["age_limit"] = match.group(1)

                # Поиск состава участников / исполнителей
                actors_el = soup.select_one(".cast, .actors, .field-name-field-actors, .persons")
                if actors_el:
                    data["participants"] = actors_el.get_text("\n", strip=True)

                # Поиск описания
                desc_el = soup.select_one(".description, .body, .field-name-body, .performance-description")
                if desc_el:
                    data["description"] = desc_el.get_text("\n", strip=True)[:600] + "..."

                if "предварительная запись" in html_text.lower() or "ограничено" in html_text.lower():
                    data["is_preorder"] = True
    except Exception as e:
        logger.warning(f"Не удалось распарсить детали страницы {detail_url}: {e}")
    
    return data


async def parse_chehov_theatre(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://www.chehovsky.ru/afishateatra/"
    base_url = "https://www.chehovsky.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                return events

            html_content = await response.text()
            soup = BeautifulSoup(html_content, "html.parser")

            cards = soup.select(".afisha-item, .event-item, .performance-item, tr.afisha_row, .views-row")

            for card in cards:
                title_el = card.select_one(".title, .name, h3, h4, .afisha_title, a")
                date_el = card.select_one(".date, .afisha_date, .day")
                time_el = card.select_one(".time, .afisha_time")
                price_el = card.select_one(".price, .afisha_price")
                link_el = card.select_one("a[href]")

                if title_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True) if date_el else ""
                    time_str = time_el.get_text(strip=True) if time_el else ""
                    prices = price_el.get_text(strip=True) if price_el else "от 300 ₽"

                    tickets_url = url
                    detail_data = {}
                    if link_el and link_el.get("href"):
                        href = link_el["href"]
                        tickets_url = urljoin(base_url, href)
                        # Заходим внутрь страницы мероприятия
                        detail_data = await parse_chehov_detail(session, tickets_url)

                    # Определение типа
                    event_type = "СПЕКТАКЛЬ"
                    title_lower = title.lower()
                    if "концерт" in title_lower or "джаз" in title_lower or "дуэт" in title_lower:
                        event_type = "КОНЦЕРТ"
                    elif "мастер-класс" in title_lower:
                        event_type = "МАСТЕР-КЛАСС"

                    event_id = f"chehov_{hash(title + date_str)}"

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.THEATRE,
                            event_type=event_type,
                            title=title,
                            age_limit=detail_data.get("age_limit", "12+"),
                            date_str=date_str,
                            time_str=time_str,
                            location="Театр им. А.П. Чехова",
                            address="ул. Петровская, 90",
                            participants=detail_data.get("participants", ""),
                            description=detail_data.get("description", ""),
                            prices=prices,
                            phone="+7 (8634) 38-29-68",
                            is_preorder_required=detail_data.get("is_preorder", False),
                            tickets_url=tickets_url,
                            image_url=detail_data.get("image_url")
                        )
                    )
    except Exception as e:
        logger.error(f"Ошибка парсинга Театра Чехова: {e}")

    return events


# ===================== ПАРСИНГ: ТАГАНРОГСКИЙ МУЗЕЙ (ТГЛИАМЗ) =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    """Глубокий парсинг страницы музея/выставки"""
    data = {
        "description": "",
        "participants": "",
        "phone": "+7 (8634) 38-34-96",
        "image_url": None,
        "is_preorder": False
    }
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")

                img_el = soup.select_one(".detail-image img, .news-detail img, .content img")
                if img_el and img_el.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_el["src"])

                desc_el = soup.select_one(".news-detail, .detail-text, .content")
                if desc_el:
                    text = desc_el.get_text("\n", strip=True)
                    data["description"] = text[:600] + "..."

                if "предварительная запись" in html_text.lower() or "запись по телефону" in html_text.lower():
                    data["is_preorder"] = True

                phone_match = re.search(r"(\+7\s?\(?\d{3,4}\)?\s?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2})", html_text)
                if phone_match:
                    data["phone"] = phone_match.group(1)
    except Exception as e:
        logger.warning(f"Ошибка деталей ТГЛИАМЗ {detail_url}: {e}")
    
    return data


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                return events

            html_content = await response.text()
            soup = BeautifulSoup(html_content, "html.parser")

            items = soup.select(".news-item, .event-card, .calendar-item, .item, .col-md-4")

            for item in items:
                title_el = item.select_one(".title, .name, h2, h3, h4, a")
                date_el = item.select_one(".date, .time")
                loc_el = item.select_one(".location, .place, .museum-title")
                link_el = item.select_one("a[href]")

                if title_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True) if date_el else "В течение месяца"
                    location = loc_el.get_text(strip=True) if loc_el else "Дворец Алфераки / Музеи Таганрога"

                    tickets_url = url
                    detail_data = {}
                    if link_el and link_el.get("href"):
                        href = link_el["href"]
                        tickets_url = urljoin(base_url, href)
                        detail_data = await parse_tgliamz_detail(session, tickets_url)

                    # Определяем тип события
                    event_type = "ВЫСТАВКА"
                    title_lower = title.lower()
                    if "мастер-класс" in title_lower or "занятие" in title_lower:
                        event_type = "МАСТЕР-КЛАСС"
                    elif "концерт" in title_lower or "музыка" in title_lower:
                        event_type = "КОНЦЕРТ"
                    elif "экскурсия" in title_lower:
                        event_type = "ЭКСКУРСИЯ"

                    event_id = f"tgliamz_{hash(title + date_str)}"

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            event_type=event_type,
                            title=title,
                            age_limit="6+",
                            date_str=date_str,
                            location=location,
                            description=detail_data.get("description", ""),
                            prices="Уточняйте в кассе",
                            phone=detail_data.get("phone", "+7 (8634) 38-34-96"),
                            is_preorder_required=detail_data.get("is_preorder", False),
                            tickets_url=tickets_url,
                            image_url=detail_data.get("image_url")
                        )
                    )
    except Exception as e:
        logger.error(f"Ошибка парсинга Музеев (ТГЛИАМЗ): {e}")

    return events


async def fetch_events() -> List[Event]:
    all_events = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        chehov_events = await parse_chehov_theatre(session)
        all_events.extend(chehov_events)

        museum_events = await parse_tgliamz_museums(session)
        all_events.extend(museum_events)

    return all_events


# ===================== ОСНОВНЫЙ ЦИКЛ ОТПРАВКИ =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены!")
        return

    bot = Bot(token=bot_token)
    db = Database()

    logger.info("Запуск парсинга с глубокой детализацией...")
    events = await fetch_events()
    logger.info(f"Найдено мероприятий: {len(events)}")

    for event in events:
        if db.is_sent(event.event_id):
            continue

        caption = format_caption(event)

        try:
            if event.image_url:
                await bot.send_photo(
                    chat_id=channel_id,
                    photo=event.image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=channel_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )

            db.mark_as_sent(event.event_id)
            logger.info(f"Отправлено: [{event.event_type}] {event.title}")
            await asyncio.sleep(2)

        except TelegramError as e:
            logger.error(f"Ошибка Telegram при отправке [{event.title}]: {e}")
        except Exception as e:
            logger.error(f"Ошибка: {e}")

    logger.info("Парсинг завершен.")


if __name__ == "__main__":
    asyncio.run(main())
