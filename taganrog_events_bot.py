import asyncio
import logging
import os
import re
import sqlite3
import html
import io
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

# ТОЧНЫЙ СПИСОК СУВЕНИРНЫХ СТОП-СЛОВ (Без ложных срабатываний на мастер-классы!)
STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры",
    "товарная лавка", "музейный магазин", "прейскурант цен на товары",
    "цена кружки", "купить открытку", "каталог сувениров"
]


class Category(Enum):
    THEATRE = "THEATRE"
    MUSEUM = "MUSEUM"


@dataclass
class Event:
    event_id: str
    category: Category
    event_type: str
    title: str
    age_limit: str = ""
    date_str: str = ""
    time_str: str = ""
    location: str = ""
    address: str = ""
    description: str = ""
    prices: str = ""
    phone: str = ""
    is_preorder_required: bool = False
    tickets_url: str = ""
    image_url: Optional[str] = None


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

    def clear_all(self):
        """Очистка базы для проверки"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sent_events")
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


# ===================== ИДЕАЛЬНОЕ ФОРМАТИРОВАНИЕ СООБЩЕНИЯ =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    event_type = html.escape(event.event_type.strip())
    
    # Возраст показываем ТОЛЬКО если он действительно есть на странице
    age_str = f" ({html.escape(event.age_limit.strip())})" if event.age_limit else ""
    
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    description = html.escape(event.description.strip())
    prices = html.escape(event.prices.strip())
    phone = html.escape(event.phone.strip())
    tickets_url = html.escape(event.tickets_url.strip())

    lines = []

    # 1. Шапка
    if event.category == Category.THEATRE:
        lines.append("<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
    else:
        lines.append("<b>ТАГАНРОГСКИЙ МУЗЕЙ-ЗАПОВЕДНИК</b>")
    
    lines.append(f"<i>{event_type}{age_str}</i>\n")
    lines.append(f"<b>{title}</b>\n")

    # 2. Дата и время
    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")

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

    # 5. Описание (курсивом)
    if description:
        short_desc = description[:350] + "..." if len(description) > 350 else description
        lines.append(f"\n<i>{short_desc}</i>")

    # 6. Кликабельный телефон с новой строки
    if phone:
        clean_phone = re.sub(r"[^\d+]", "", phone)
        lines.append(f"\n<b>Телефон для справок:</b>\n<a href='tel:{clean_phone}'>{phone}</a>")

    # 7. Ссылка на событие
    if tickets_url:
        action_text = "Записаться на мероприятие" if event.is_preorder_required else "Подробности и билеты"
        lines.append(f"\n👉 <a href='{tickets_url}'><b>{action_text}</b></a>")

    # 8. Хэштеги
    tags = ["#Таганрог", "#АфишаТаганрог"]
    if event.category == Category.THEATRE:
        tags.extend(["#ТеатрЧехова", "#Театр"])
    else:
        tags.extend(["#Музей", "#Выставка"])

    lines.append("\n" + " ".join(tags))

    return "\n".join(lines)


def is_souvenir(title: str, text: str) -> bool:
    check_str = (title + " " + text).lower()
    return any(word in check_str for word in STRICT_SOUVENIR_WORDS)


# ===================== ПАРСИНГ ДЕТАЛЕЙ МУЗЕЯ (ТГЛИАМЗ) =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "description": "",
        "image_url": None,
        "location": "",
        "date_str": "",
        "time_str": "",
        "phone": "+7 (8634) 38-34-96",
        "age_limit": "",
        "is_souvenir": False
    }
    try:
        async with session.get(detail_url, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")

                page_title = soup.find("h1")
                title_str = page_title.get_text(strip=True) if page_title else ""
                if is_souvenir(title_str, html_text):
                    data["is_souvenir"] = True
                    return data

                # Ищем полноразмерную афишу из Битрикса (/upload/)
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if "/upload/" in src and not any(x in src for x in ["icon", "logo", "menu", "banner"]):
                        data["image_url"] = urljoin("https://tgliamz.ru", src)
                        break

                # Извлечение даты и времени из текста страницы
                date_match = re.search(r"(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4})?)", html_text, re.IGNORECASE)
                if date_match:
                    data["date_str"] = date_match.group(1)

                time_match = re.search(r"(\b\d{1,2}[:.-]\d{2}\b|\b\d{1,2}\s*ч(?:асов)?\b)", html_text, re.IGNORECASE)
                if time_match:
                    data["time_str"] = time_match.group(1)

                # Поиск филиала музея
                content_block = soup.select_one(".content, .news-detail, .workarea, #content")
                if content_block:
                    paragraphs = [p.get_text(strip=True) for p in content_block.find_all("p") if len(p.get_text(strip=True)) > 15]
                    if paragraphs:
                        data["description"] = "\n\n".join(paragraphs[:2])

                # Поиск возраста
                age_match = re.search(r"\b(\d{1,2}\+)\b", html_text)
                if age_match:
                    data["age_limit"] = age_match.group(1)

                # Поиск локального телефона филиала
                phone_match = re.search(r"(\+?7|8)[\s\(\-]*\d{3}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}", html_text)
                if phone_match:
                    data["phone"] = phone_match.group(0)

    except Exception as e:
        logger.warning(f"Ошибка деталей [{detail_url}]: {e}")
    
    return data


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://tgliamz.ru/calendar/index.php"
    base_url = "https://tgliamz.ru"

    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                html_content = await response.text()
                soup = BeautifulSoup(html_content, "html.parser")

                links = soup.find_all("a", href=re.compile(r"detaill\.php|ELEMENT_ID="))
                visited_urls = set()

                for link in links:
                    href = link.get("href")
                    if not href:
                        continue

                    full_url = urljoin(base_url, href)
                    if full_url in visited_urls:
                        continue
                    visited_urls.add(full_url)

                    title = link.get_text(strip=True)
                    if len(title) < 4 or "подробнее" in title.lower():
                        parent = link.find_parent(["div", "tr", "li", "td"])
                        if parent:
                            title_el = parent.find(["h2", "h3", "h4", "b", "strong"])
                            if title_el:
                                title = title_el.get_text(strip=True)

                    if len(title) < 4:
                        continue

                    detail_data = await parse_tgliamz_detail(session, full_url)

                    if detail_data["is_souvenir"]:
                        logger.info(f"Пропущен сувенир: {title}")
                        continue

                    # Определяем тип мероприятия
                    event_type = "Выставка"
                    t_lower = title.lower()
                    if "концерт" in t_lower or "джаз" in t_lower or "музыка" in t_lower:
                        event_type = "Концерт"
                    elif "мастер-класс" in t_lower or "пленэр" in t_lower:
                        event_type = "Мастер-класс"
                    elif "лекция" in t_lower:
                        event_type = "Лекция"

                    elem_id_match = re.search(r"ELEMENT_ID=(\d+)", full_url)
                    event_id = f"tgliamz_{elem_id_match.group(1)}" if elem_id_match else f"tgliamz_{hash(title)}"

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            event_type=event_type,
                            title=title,
                            age_limit=detail_data["age_limit"],
                            date_str=detail_data["date_str"],
                            time_str=detail_data["time_str"],
                            location=detail_data["location"] or "Таганрогский музей-заповедник",
                            description=detail_data["description"],
                            phone=detail_data["phone"],
                            tickets_url=full_url,
                            image_url=detail_data["image_url"]
                        )
                    )
    except Exception as e:
        logger.error(f"Ошибка афиши ТГЛИАМЗ: {e}")

    return events


# ===================== ПАРСИНГ ТЕАТРА ЧЕХОВА =====================
async def parse_chekhov_theatre(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    url = "https://www.chekhovteatr.rus/afisha"
    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                html_content = await response.text()
                soup = BeautifulSoup(html_content, "html.parser")
                
                # Поиск спектаклей на сайте театра
                items = soup.select(".afisha-item, .event-card, .performance")
                for item in items:
                    title_el = item.select_one(".title, h3, h2")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    
                    link_el = item.find("a")
                    tickets_url = urljoin(url, link_el["href"]) if link_el and link_el.get("href") else url

                    img_el = item.find("img")
                    image_url = urljoin(url, img_el["src"]) if img_el and img_el.get("src") else None

                    events.append(
                        Event(
                            event_id=f"theatre_{hash(title)}",
                            category=Category.THEATRE,
                            event_type="Спектакль",
                            title=title,
                            address="ул. Петровская, 90",
                            phone="+7 (8634) 38-35-73",
                            tickets_url=tickets_url,
                            image_url=image_url
                        )
                    )
    except Exception as e:
        logger.warning(f"Ошибка парсинга Театра Чехова: {e}")
    return events


# ===================== ЗАПУСК БОТА =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: Токены не найдены!")
        return

    bot = Bot(token=bot_token)
    db = Database()
    db.clear_all()  # Сброс базы для чистой проверки отправки!

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        logger.info("Запуск профессионального парсинга...")
        
        museum_events = await parse_tgliamz_museums(session)
        theatre_events = await parse_chekhov_theatre(session)
        
        events = museum_events + theatre_events
        logger.info(f"Найдено уникальных мероприятий: {len(events)}")

        for event in events:
            if db.is_sent(event.event_id):
                continue

            caption = format_caption(event)
            photo_sent = False

            if event.image_url:
                try:
                    async with session.get(event.image_url, timeout=12) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            if len(img_bytes) > 3000:
                                img_file = io.BytesIO(img_bytes)
                                img_file.name = "poster.jpg"

                                await bot.send_photo(
                                    chat_id=channel_id,
                                    photo=img_file,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML
                                )
                                photo_sent = True
                                logger.info(f"Успешно отправлено с фото: {event.title}")
                except Exception as e:
                    logger.warning(f"Ошибка отправки фото [{event.image_url}]: {e}")

            if not photo_sent:
                try:
                    await bot.send_message(
                        chat_id=channel_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    logger.info(f"Успешно отправлено без фото: {event.title}")
                except TelegramError as e:
                    logger.error(f"Ошибка отправки текста: {e}")

            db.mark_as_sent(event.event_id)
            await asyncio.sleep(3)

    logger.info("Парсинг успешно завершен.")


if __name__ == "__main__":
    asyncio.run(main())
