import asyncio
import html
import logging
import os
import sqlite3
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
# Иконки для категорий и полей (можно легко заменить или выключить)
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

    # Поля деталей
    "date": "📅 ",
    "time": "🕐 ",
    "location": "📍 ",
    "description": "📝 ",
    "price": "💰 ",
    "ticket": "🎟 ",
    "phone": "📞 ",
}

# Чтобы полностью отключить эмодзи, раскомментируйте строчку ниже:
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


# ===================== БАЗА ДАННЫХ (ИСКЛЮЧЕНИЕ ДУБЛЕЙ) =====================
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


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =====================
def format_caption(event: Event) -> str:
    # Безопасное экранирование специальных символов для HTML в Telegram
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

    # 1. Шапка категории
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

    # 2. Название мероприятия
    lines.append(f"\n<b>{title}</b>\n")

    # 3. Дата и время
    date_time_parts = []
    if date_str:
        date_time_parts.append(f"{ICONS['date']}{date_str}")
    if time_str:
        date_time_parts.append(f"{ICONS['time']}{time_str}")
    if date_time_parts:
        lines.append(" | ".join(date_time_parts))

    # 4. Локация и адрес
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

    # 7. Билеты и телефон
    contact_parts = []
    if tickets_url:
        contact_parts.append(f"{ICONS['ticket']}<a href='{tickets_url}'>Купить билет / Подробнее</a>")
    if phone:
        contact_parts.append(f"{ICONS['phone']}{phone}")
    
    if contact_parts:
        lines.append("\n" + "\n".join(contact_parts))

    # 8. Тематические хэштеги
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


# ===================== СБОР СОБЫТИЙ (ПАРСИНГ) =====================
async def fetch_events() -> List[Event]:
    """
    Основная функция для сбора данных с сайтов.
    Замените или дополните этот блок вашими BeautifulSoup-селекторами.
    """
    events: List[Event] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # Пример: Сбор афиши театра им. Чехова
        try:
            url = "https://www.chehovsky.ru/"
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, "html.parser")
                    
                    # Логика вашего парсера добавляется сюда...
                    # Ниже тестовый элемент для проверки работоспособности:
                    events.append(
                        Event(
                            event_id="chehov_demo_001",
                            category=Category.THEATRE_TODAY,
                            title="Спектакль «Палата №6»",
                            date_str="Сегодня",
                            time_str="19:00",
                            location="Театр им. А.П. Чехова",
                            address="ул. Петровская, 90",
                            description="Постановка классического произведения А.П. Чехова.",
                            prices="от 400 руб.",
                            phone="+7 (8634) 38-29-68",
                            tickets_url="https://www.chehovsky.ru/",
                            image_url=None
                        )
                    )
        except Exception as e:
            logger.error(f"Ошибка при сборе данных с {url}: {e}")

    return events


# ===================== ОСНОВНОЙ ЦИКЛ ЗАПУСКА =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы в переменных окружения!")
        return

    bot = Bot(token=bot_token)
    db = Database()

    logger.info("Бот запущен. Сбор актуальных событий...")
    events = await fetch_events()
    logger.info(f"Найдено событий для проверки: {len(events)}")

    for event in events:
        # Пропускаем, если событие уже отправлялось ранее
        if db.is_sent(event.event_id):
            logger.info(f"Событие [{event.event_id}] уже было отправлено, пропускаем.")
            continue

        caption = format_caption(event)

        try:
            # Отправка с фото или только текстом
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

            # Отмечаем как отправленное
            db.mark_as_sent(event.event_id)
            logger.info(f"Успешно опубликовано: {event.title}")

            # Пауза 2 секунды, чтобы соблюдать лимиты Telegram API
            await asyncio.sleep(2)

        except TelegramError as e:
            logger.error(f"Ошибка Telegram API при отправке [{event.event_id}]: {e}")
        except Exception as e:
            logger.error(f"Неизвестная ошибка при отправке [{event.event_id}]: {e}")

    logger.info("Выполнение задачи завершено.")


if __name__ == "__main__":
    asyncio.run(main())
