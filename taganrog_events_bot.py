import asyncio
import logging
import os
import sqlite3
import html
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

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

# ===================== НАСТРОЙКИ СТИЛЯ И ИКОНОК =====================
# Вы можете изменить любую иконку, заменить на символы или оставить пустыми ""
ICONS = {
    # Категории
    "cat_theatre_month": "🎭 ",
    "cat_theatre_today": "🎭 ",
    "cat_cinema": "🎬 ",
    "cat_museum": "🎨 ",
    "cat_events": "🎪 ",
    "cat_greenwich": "🌊 ",
    "cat_aqualazur": "🎢 ",
    "cat_golden_horse": "🐴 ",

    # Поля событий
    "date": "📅 ",
    "time": "🕐 ",
    "location": "📍 ",
    "description": "📝 ",
    "price": "💰 ",
    "ticket": "🎟 ",
    "phone": "📞 ",
}

# Чтобы полностью отключить иконки (сделать чистый текст), раскомментируйте строчку ниже:
# ICONS = {k: "" for k in ICONS}


# ===================== МОДЕЛИ ДАННЫХ =====================
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
    event_id: str
    category: Category
    title: str
    date_str: str = ""
    time_str: str = ""
    location: str = ""
    address: str = ""
    description: str = ""
    prices: str = ""
    phone: str = ""
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


# ===================== ФОРМАТТИРОВАНИЕ СООБЩЕНИЙ =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title)
    date_str = html.escape(event.date_str)
    time_str = html.escape(event.time_str)
    location = html.escape(event.location)
    address = html.escape(event.address)
    description = html.escape(event.description)
    prices = html.escape(event.prices)
    phone = html.escape(event.phone)
    tickets_url = html.escape(event.tickets_url)

    lines = []

    # 1. Заголовок категории
    category_titles = {
        Category.THEATRE_MONTH: f"{ICONS['cat_theatre_month']}<b>АФИША ТЕАТРА НА МЕСЯЦ</b>",
        Category.THEATRE_TODAY: f"{ICONS['cat_theatre_today']}<b>ТЕАТР СЕГОДНЯ</b>",
        Category.CINEMA: f"{ICONS['cat_cinema']}<b>КИНО В ТАГАНРОГЕ</b>",
        Category.MUSEUM: f"{ICONS['cat_museum']}<b>МУЗЕИ И ВЫСТАВКИ</b>",
        Category.EVENTS: f"{ICONS['cat_events']}<b>СОБЫТИЯ И КОНЦЕРТЫ</b>",
        Category.GREENWICH: f"{ICONS['cat_greenwich']}<b>ГРИНВИЧ ПАРК SPA</b>",
        Category.AQUALAZUR: f"{ICONS['cat_aqualazur']}<b>АКВАПАРК «ЛАЗУРНЫЙ»</b>",
        Category.GOLDEN_HORSE: f"{ICONS['cat_golden_horse']}<b>КЛУБ «ГОЛДЕН ХОРС»</b>",
    }
    
    header = category_titles.get(event.category, "<b>АФИША ТАГАНРОГА</b>")
    lines.append(header)

    # 2. Название события
    lines.append(f"\n<b>{title}</b>\n")

    # 3. Дата и время
    date_time_parts = []
    if date_str:
        date_time_parts.append(f"{ICONS['date']}{date_str}")
    if time_str:
        date_time_parts.append(f"{ICONS['time']}{time_str}")
    if date_time_parts:
        lines.append(" | ".join(date_time_parts))

    # 4. Место и адрес
    loc_icon = ICONS['location']
    if location and address:
        lines.append(f"{loc_icon}{location} ({address})")
    elif location:
        lines.append(f"{loc_icon}{location}")
    elif address:
        lines.append(f"{loc_icon}{address}")

    # 5. Описание
    if description:
        lines.append(f"\n{ICONS['description']}{description}")

    # 6. Стоимость
    if prices:
        lines.append(f"{ICONS['price']}{prices}")

    # 7. Ссылки и контакты
    contact_parts = []
    if tickets_url:
        contact_parts.append(f"{ICONS['ticket']}<a href='{tickets_url}'>Купить билет / Подробнее</a>")
    if phone:
        contact_parts.append(f"{ICONS['phone']}{phone}")
    
    if contact_parts:
        lines.append("\n" + "\n".join(contact_parts))

    # 8. Хэштеги
    hashtags = {
        Category.THEATRE_MONTH: "#Таганрог #Театр #Афиша",
        Category.THEATRE_TODAY: "#Таганрог #Театр #Спектакль",
        Category.CINEMA: "#Таганрог #Кино #Афиша",
        Category.MUSEUM: "#Таганрог #Музей #Выставка",
        Category.EVENTS: "#Таганрог #Афиша #Концерт",
        Category.GREENWICH: "#Таганрог #Отдых #SPA",
        Category.AQUALAZUR: "#Таганрог #Отдых #Аквапарк",
        Category.GOLDEN_HORSE: "#Таганрог #Развлечения",
    }
    
    lines.append(f"\n{hashtags.get(event.category, '#Таганрог #Афиша')}")

    return "\n".join(lines)


# ===================== ПАРСИНГ СОБЫТИЙ (ЗАГЛУШКА/ПРИМЕР) =====================
async def fetch_events() -> List[Event]:
    """
    Здесь находится ваша логика парсинга сайтов (BeautifulSoup / aiohttp).
    Возвращает список объектa Event.
    """
    events = []
    
    # Пример тестового события (можно удалить при подключении реального парсера)
    events.append(
        Event(
            event_id="test_event_001",
            category=Category.THEATRE_TODAY,
            title="Спектакль «Палата №6»",
            date_str="Сегодня 29.07",
            time_str="19:00",
            location="Театр им. А.П. Чехова",
            address="ул. Петровская, 90",
            description="Классическая постановка по знаменитой повести А.П. Чехова.",
            prices="от 400 руб.",
            phone="+7 (8634) 38-29-68",
            tickets_url="https://www.chehovsky.ru/",
            image_url=None
        )
    )
    
    return events


# ===================== ОСНОВНАЯ ЛОГИКА ОТПРАВКИ =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: Переменные TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не найдены!")
        return

    bot = Bot(token=bot_token)
    db = Database()

    logger.info("Начало работы бота: сбор событий...")
    events = await fetch_events()
    logger.info(f"Найдено событий: {len(events)}")

    for event in events:
        if db.is_sent(event.event_id):
            logger.info(f"Событие {event.event_id} уже отправлялось, пропускаем.")
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
            logger.info(f"Успешно отправлено: {event.title}")
            
            # Небольшая пауза между постами, чтобы Telegram не заблокировал за спам
            await asyncio.sleep(2)

        except TelegramError as e:
            logger.error(f"Ошибка при отправке события {event.event_id}: {e}")
        except Exception as e:
            logger.error(f"Неизвестная ошибка: {e}")

    logger.info("Работа бота завершена.")


if __name__ == "__main__":
    asyncio.run(main())
