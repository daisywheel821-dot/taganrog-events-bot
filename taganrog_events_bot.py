import asyncio
import logging
import os
import sqlite3
import html
import io
import re
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

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры",
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
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


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_souvenir_shop_item(text: str) -> bool:
    check_str = text.lower()
    return any(word in check_str for word in STRICT_SOUVENIR_WORDS)


def format_phone_number(raw_phone: str) -> tuple[str, str]:
    """
    Возвращает пару: (красивое отображение, чистый номер для tel:)
    Пример: '+7 (8634) 38-34-96', '+78634383496'
    """
    digits = re.sub(r"[^\d]", "", raw_phone)
    if not digits:
        return raw_phone, raw_phone

    if len(digits) == 11:
        # Корректируем код страны
        country_code = "+7" if digits.startswith("7") or digits.startswith("8") else f"+{digits[0]}"
        area = digits[1:5]
        p1 = digits[5:7]
        p2 = digits[7:9]
        p3 = digits[9:11]
        
        display_phone = f"{country_code} ({area}) {p1}-{p2}-{p3}"
        tel_phone = f"+7{digits[1:]}"
        return display_phone, tel_phone
    elif len(digits) == 6:  # Городской 6-значный номер Таганрога
        display_phone = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
        tel_phone = f"+78634{digits}"
        return display_phone, tel_phone

    return raw_phone, f"+{digits}"


# ===================== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title.strip())
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    description = html.escape(event.description.strip())
    prices = html.escape(event.prices.strip())
    tickets_url = html.escape(event.tickets_url.strip())

    lines = []

    if event.category == Category.THEATRE_MONTH:
        lines.append("<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
        lines.append("<i>Репертуар и анонс спектаклей</i>\n")
    elif event.category == Category.MUSEUM:
        lines.append("<b>МУЗЕИ И ВЫСТАВКИ ТАГАНРОГА</b>")
        lines.append("<i>Таганрогский музей-заповедник</i>\n")

    lines.append(f"<b>{title}</b>\n")

    date_parts = []
    if date_str:
        date_parts.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        date_parts.append(f"<b>Время:</b> {time_str}")
    if date_parts:
        lines.append(" | ".join(date_parts))

    if location and address:
        lines.append(f"<b>Место:</b> {location} ({address})")
    elif location:
        lines.append(f"<b>Место:</b> {location}")
    elif address:
        lines.append(f"<b>Адрес:</b> {address}")

    if prices:
        lines.append(f"<b>Стоимость:</b> {prices}")

    if description:
        lines.append(f"\n{description}")

    links = []
    if tickets_url:
        links.append(f"<a href='{tickets_url}'>Официальная страница / Билеты</a>")
    
    if event.phone:
        display_phone, tel_phone = format_phone_number(event.phone)
        links.append(f"<b>Справки по телефону:</b> <a href='tel:{tel_phone}'>{display_phone}</a>")

    if links:
        lines.append("\n" + "\n".join(links))

    if event.category == Category.THEATRE_MONTH:
        lines.append("\n#Таганрог #ТеатрЧехова #Афиша")
    else:
        lines.append("\n#Таганрог #Музей #Выставка")

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

                        tickets_url = url
                        if link_el and link_el.get("href"):
                            tickets_url = urljoin(base_url, link_el["href"])

                        image_url = None
                        if img_el and img_el.get("src"):
                            image_url = urljoin(base_url, img_el["src"])

                        event_id = f"chehov_{hash(title + date_str + tickets_url)}"

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


async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    """Извлечение подробного описания, даты и телефона со страницы события ТГЛИАМЗ"""
    data = {"description": "", "date_str": "", "time_str": "", "phone": "+7 (8634) 38-34-96", "is_shop": False}
    try:
        async with session.get(detail_url, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data

                soup = BeautifulSoup(html_text, "html.parser")
                
                # Улучшенный сбор текста для Bitrix страниц ТГЛИАМЗ
                content_block = soup.select_one(".detail-text, .news-detail, .content-text, .detail_text, .workarea")
                if content_block:
                    # Удаляем ненужные теги скриптов и стилей
                    for s in content_block(["script", "style"]):
                        s.extract()
                    
                    paragraphs = []
                    for el in content_block.find_all(["p", "div"]):
                        txt = el.get_text(strip=True)
                        # Фильтруем служебный текст и короткие строки
                        if len(txt) > 30 and not txt.startswith("Тел") and not txt.startswith("Купить"):
                            if txt not in paragraphs:
                                paragraphs.append(txt)
                    
                    if paragraphs:
                        # Берем первые 2 смысловых абзаца
                        data["description"] = "\n\n".join(paragraphs[:2])

                # Извлечение даты
                date_match = re.search(r"(\d{1,2}(?:\s*-\s*\d{1,2})?\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", html_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1)

                # Точный поиск телефона музея на странице
                phone_match = re.search(r"(\+?7|8)[\s\(\-]*\(?8634\)?[\s\(\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}", html_text)
                if phone_match:
                    data["phone"] = phone_match.group(0)

    except Exception as e:
        logger.warning(f"Ошибка получения деталей страницы {detail_url}: {e}")
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

                items = soup.select(".news-item, .event-card, .calendar-item, .item, .col-md-4, .col-sm-6")
                for item in items:
                    title_el = item.select_one(".title, .name, h2, h3, h4")
                    date_el = item.select_one(".date, .time")
                    loc_el = item.select_one(".location, .place, .museum-title")
                    img_el = item.select_one("img")
                    link_el = item.select_one("a[href]")

                    if title_el:
                        title = title_el.get_text(strip=True)
                        if len(title) < 3 or "подробнее" in title.lower():
                            continue

                        date_str = date_el.get_text(strip=True) if date_el else ""
                        location = loc_el.get_text(strip=True) if loc_el else "Таганрогский музей-заповедник"

                        tickets_url = url
                        if link_el and link_el.get("href"):
                            tickets_url = urljoin(base_url, link_el["href"])

                        # Подтягиваем детали страницы
                        detail_data = await parse_tgliamz_detail(session, tickets_url)
                        if detail_data["is_shop"]:
                            continue

                        image_url = None
                        if img_el and img_el.get("src"):
                            src = img_el["src"]
                            image_url = urljoin(base_url, src)

                        event_id = f"tgliamz_{hash(title + (date_str or detail_data['date_str']) + tickets_url)}"

                        events.append(
                            Event(
                                event_id=event_id,
                                category=Category.MUSEUM,
                                title=title,
                                date_str=date_str or detail_data["date_str"],
                                location=location,
                                description=detail_data["description"],
                                phone=detail_data["phone"],
                                tickets_url=tickets_url,
                                image_url=image_url
                            )
                        )
    except Exception as e:
        logger.error(f"Ошибка парсинга Музеев (ТГЛИАМЗ): {e}")

    return events


async def fetch_events(session: aiohttp.ClientSession) -> List[Event]:
    all_events = []
    chehov_events = await parse_chehov_theatre(session)
    all_events.extend(chehov_events)

    museum_events = await parse_tgliamz_museums(session)
    all_events.extend(museum_events)

    return all_events


# ===================== ОСНОВНОЙ ЦИКЛ ОТПРАВКИ =====================
async def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

    if not bot_token or not channel_id:
        logger.error("ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены!")
        return

    bot = Bot(token=bot_token)
    db = Database()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        logger.info("Запуск Парсинга...")
        events = await fetch_events(session)
        logger.info(f"Всего найдено мероприятий: {len(events)}")

        for event in events:
            if db.is_sent(event.event_id):
                logger.info(f"Событие [{event.title}] уже было отправлено, пропускаем.")
                continue

            caption = format_caption(event)
            photo_sent = False

            # Пробуем скачать картинку напрямую в память и отправить её
            if event.image_url:
                try:
                    async with session.get(event.image_url, timeout=10) as img_resp:
                        if img_resp.status == 200:
                            img_data = await img_resp.read()
                            img_file = io.BytesIO(img_data)
                            img_file.name = "image.jpg"

                            await bot.send_photo(
                                chat_id=channel_id,
                                photo=img_file,
                                caption=caption,
                                parse_mode=ParseMode.HTML
                            )
                            photo_sent = True
                except Exception as img_err:
                    logger.warning(f"Не удалось загрузить фото для [{event.title}], отправляем текстом: {img_err}")

            # Если фото не отправлено (нет картинки или ошибка загрузки), отправляем текстом
            if not photo_sent:
                try:
                    await bot.send_message(
                        chat_id=channel_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except TelegramError as e:
                    logger.error(f"Ошибка отправки текста для [{event.title}]: {e}")
                    continue

            db.mark_as_sent(event.event_id)
            logger.info(f"Успешно отправлено в Telegram: {event.title}")

            # Пауза 2 секунды между постами
            await asyncio.sleep(2)

    logger.info("Запуск завершен.")


if __name__ == "__main__":
    asyncio.run(main())
