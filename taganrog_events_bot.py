import asyncio
import logging
import os
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
    THEATRE_MONTH = "THEATRE_MONTH"
    MUSEUM = "MUSEUM"


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


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ (БЕЗ ЭМОДЗИ) =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    description = html.escape(event.description.strip())
    prices = html.escape(event.prices.strip())
    phone = html.escape(event.phone.strip())
    tickets_url = html.escape(event.tickets_url.strip())

    lines = []

    # 1. Заголовок категории (строгий стиль)
    if event.category == Category.THEATRE_MONTH:
        lines.append("<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
        lines.append("<i>Репертуар и анонс спектаклей</i>\n")
    elif event.category == Category.MUSEUM:
        lines.append("<b>МУЗЕИ И ВЫСТАВКИ ТАГАНРОГА</b>")
        lines.append("<i>Таганрогский музей-заповедник</i>\n")

    # 2. Название мероприятия
    lines.append(f"<b>{title}</b>\n")

    # 3. Дата и время
    date_parts = []
    if date_str:
        date_parts.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        date_parts.append(f"<b>Время:</b> {time_str}")
    if date_parts:
        lines.append(" | ".join(date_parts))

    # 4. Локация и адрес
    if location and address:
        lines.append(f"<b>Место:</b> {location} ({address})")
    elif location:
        lines.append(f"<b>Место:</b> {location}")
    elif address:
        lines.append(f"<b>Адрес:</b> {address}")

    # 5. Стоимость
    if prices:
        lines.append(f"<b>Стоимость:</b> {prices}")

    # 6. Краткое описание (если есть)
    if description:
        lines.append(f"\n{description}")

    # 7. Контакты и ссылка на билеты
    links = []
    if tickets_url:
        links.append(f"<a href='{tickets_url}'>Официальная страница / Билеты</a>")
    if phone:
        links.append(f"<b>Справки по телефону:</b> {phone}")

    if links:
        lines.append("\n" + "\n".join(links))

    # 8. Хэштеги
    if event.category == Category.THEATRE_MONTH:
        lines.append("\n#Таганрог #ТеатрЧехова #Афиша")
    else:
        lines.append("\n#Таганрог #Музей #Выставка")

    return "\n".join(lines)


# ===================== МОДУЛИ ПАРСИНГА =====================
async def parse_chehov_theatre(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://www.chehovsky.ru/afishateatra/"
    base_url = "https://www.chehovsky.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                logger.error(f"Не удалось открыть сайт театра: статус {response.status}")
                return events

            html_content = await response.text()
            soup = BeautifulSoup(html_content, "html.parser")

            # Поиск карточек спектаклей в афише
            cards = soup.select(".afisha-item, .event-item, .performance-item, tr.afisha_row")
            if not cards:
                # Альтернативный поиск по таблицам/блокам расписания
                cards = soup.find_all("div", class_=lambda c: c and "afisha" in c)

            for card in cards:
                title_el = card.select_one(".title, .name, h3, h4, .afisha_title")
                date_el = card.select_one(".date, .afisha_date")
                time_el = card.select_one(".time, .afisha_time")
                price_el = card.select_one(".price, .afisha_price")
                img_el = card.select_one("img")
                link_el = card.select_one("a[href]")

                if title_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True) if date_el else ""
                    time_str = time_el.get_text(strip=True) if time_el else ""
                    prices = price_el.get_text(strip=True) if price_el else "Уточняйте в кассе"

                    tickets_url = url
                    if link_el and link_el.get("href"):
                        tickets_url = urljoin(base_url, link_el["href"])

                    image_url = None
                    if img_el and img_el.get("src"):
                        image_url = urljoin(base_url, img_el["src"])

                    # Формируем уникальный ID для базы данных
                    event_id = f"chehov_{hash(title + date_str)}"

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.THEATRE_MONTH,
                            title=title,
                            date_str=date_str,
                            time_str=time_str,
                            location="Театр им. А.П. Чехова",
                            address="ул. Петровская, 90",
                            prices=prices,
                            phone="+7 (8634) 38-29-68",
                            tickets_url=tickets_url,
                            image_url=image_url
                        )
                    )
    except Exception as e:
        logger.error(f"Ошибка парсинга Театра Чехова: {e}")

    return events


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                logger.error(f"Не удалось открыть сайт музеев: статус {response.status}")
                return events

            html_content = await response.text()
            soup = BeautifulSoup(html_content, "html.parser")

            # Поиск элементов выставок и событий
            items = soup.select(".news-item, .event-card, .calendar-item, .item")
            for item in items:
                title_el = item.select_one(".title, .name, h2, h3")
                date_el = item.select_one(".date, .time")
                loc_el = item.select_one(".location, .place, .museum-title")
                img_el = item.select_one("img")
                link_el = item.select_one("a[href]")

                if title_el:
                    title = title_el.get_text(strip=True)
                    date_str = date_el.get_text(strip=True) if date_el else "Действующая выставка"
                    location = loc_el.get_text(strip=True) if loc_el else "Таганрогский музей-заповедник"

                    tickets_url = url
                    if link_el and link_el.get("href"):
                        tickets_url = urljoin(base_url, link_el["href"])

                    image_url = None
                    if img_el and img_el.get("src"):
                        image_url = urljoin(base_url, img_el["src"])

                    event_id = f"tgliamz_{hash(title + date_str)}"

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            title=title,
                            date_str=date_str,
                            location=location,
                            tickets_url=tickets_url,
                            image_url=image_url
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
        # 1. Запрос к театру
        chehov_events = await parse_chehov_theatre(session)
        all_events.extend(chehov_events)

        # 2. Запрос к музеям
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

    logger.info("Запуск Этапа 1: Парсинг Театра Чехова и Музеев...")
    events = await fetch_events()
    logger.info(f"Всего найдено мероприятий: {len(events)}")

    for event in events:
        if db.is_sent(event.event_id):
            logger.info(f"Событие [{event.title}] уже было отправлено, пропускаем.")
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

            # Задержка 2 секунды между сообщениями для защиты от лимитов Telegram
            await asyncio.sleep(2)

        except TelegramError as e:
            logger.error(f"Ошибка отправки Telegram для [{event.title}]: {e}")
        except Exception as e:
            logger.error(f"Неизвестная ошибка: {e}")

    logger.info("Этап 1 успешно завершен.")


if __name__ == "__main__":
    asyncio.run(main())
