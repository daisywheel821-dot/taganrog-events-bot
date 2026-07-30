import asyncio
import logging
import os
import sqlite3
import html
import io
import re
from dataclasses import dataclass, field
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
    "сувенирная продукция", "купить сувенир", "в продаже сувениры",
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
]

EXCLUDED_PHONES = ["383496", "38-34-96"]

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн"],
        "name": "Литературный музей\nА.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЛитературныйМузейЧехова"
    },
    {
        "keys": ["юрнкц", "южно-российский"],
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


def extract_all_phones(text: str) -> List[tuple]:
    phone_pattern = r"(?:\+?7|8)[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}|\b\d{2}-\d{2}-\d{2}\b"
    raw_phones = re.findall(phone_pattern, text)

    formatted_phones = []
    seen_digits = set()

    for raw in raw_phones:
        digits = re.sub(r"\D", "", raw)
        if not digits:
            continue

        if any(ex in digits for ex in EXCLUDED_PHONES):
            continue

        # 6-значные городские номера (8634)
        if len(digits) == 6 and digits not in seen_digits:
            seen_digits.add(digits)
            display = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
            tel = f"+78634{digits}"
            formatted_phones.append((display, tel))

        # 11-значные номера
        elif len(digits) == 11 and digits not in seen_digits:
            seen_digits.add(digits)
            if digits[1] == '9':
                display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                tel = f"+7{digits[1:]}"
            elif digits[1:5] == '8634':
                display = f"8 (8634) {digits[5:7]}-{digits[7:9]}-{digits[9:]}"
                tel = f"+7{digits[1:]}"
            else:
                display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                tel = f"+7{digits[1:]}"
            formatted_phones.append((display, tel))

    return formatted_phones


def detect_event_type(title: str, full_text: str) -> str:
    combined = (title + " " + full_text).lower()
    if "мастер-класс" in combined or "мастер класс" in combined:
        return "Мастер-класс"
    elif "концерт" in combined or "джаз" in combined or "музыкальн" in combined:
        return "Музыкально-литературная программа"
    elif "лекци" in combined:
        return "Лекция"
    elif "экскурси" in combined:
        return "Экскурсионный сеанс"
    elif "выставк" in combined or "экспозиц" in combined:
        return "Выставка"
    elif "спектакль" in combined:
        return "Спектакль"
    return "Музейная программа"


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

    # 2. Название события
    lines.append(f"<b>«{title}»</b>\n")

    # 3. Детали (Дата, Время, Цена, Запись/Бронирование)
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

    # 5. Блок телефонов (с кликабельными tel: ссылками)
    if event.phones:
        lines.append("\n📞 <b>Справки по телефону:</b>")
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
                                event_type="Спектакль",
                                date_str=date_str,
                                time_str=time_str,
                                location="Таганрогский театр\nим. А.П. Чехова",
                                address="ул. Петровская, 90",
                                prices=prices,
                                phones=extract_all_phones("8 (8634) 38-29-68"),
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

                # 1. Поиск индивидуальной ссылки на vmuzey.com
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    if "vmuzey.com" in href:
                        data["buy_ticket_url"] = href
                        break

                # 2. Определение типа программы
                type_el = soup.select_one(".category-title, .subtitle, .event-type, .news-category, .section-title, .type")
                if type_el and len(type_el.get_text(strip=True)) > 2:
                    data["event_type"] = type_el.get_text(strip=True)
                else:
                    data["event_type"] = detect_event_type(card_title, page_full_text)

                # 3. Поиск главной картинки
                img_el = soup.select_one(".news-item-img img, .detail-image img, .news-detail img, .content img, .workarea img")
                if img_el and img_el.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_el["src"])

                # 4. Телефоны и проверка бронирования/записи
                data["phones"] = extract_all_phones(page_full_text)

                check_booking_text = page_full_text.lower()
                if any(phrase in check_booking_text for phrase in [
                    "предварительная запись обязательна", 
                    "предварительная запись",
                    "запись по телефону", 
                    "бронирование мест обязательно",
                    "бронирование мест",
                    "обязательна предварительная запись"
                ]):
                    data["requires_booking"] = True

                # 5. Определение филиала и адреса
                for branch in MUSEUM_BRANCHES:
                    if any(k in check_booking_text for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # 6. Парсинг даты, времени, цены
                date_match = re.search(r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", page_full_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()

                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", page_full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*|цена[^\d]*?)(\d+\s*руб[а-я]*[^\.\n]*)", page_full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1).strip()

    except Exception as e:
        logger.warning(f"Ошибка парсинга {detail_url}: {e}")
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

                    # Очистка названия от кавычек если они уже там есть
                    title = title.strip(" «»\"'")

                    if len(title) < 3 or "подробнее" in title.lower():
                        continue

                    date_el = item.select_one(".date, .time, .calendar-date")
                    loc_el = item.select_one(".location, .place, .museum-title")
                    img_el = item.select_one("img")
                    
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

                    image_url = detail_data["image_url"]
                    if not image_url and img_el and img_el.get("src"):
                        image_url = urljoin(base_url, img_el["src"])

                    final_location = detail_data["location"] or location_card
                    final_tags = generate_museum_tags(
                        title, 
                        detail_data["branch_tag"]
                    )

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            title=title,
                            event_type=detail_data["event_type"],
                            date_str=detail_data["date_str"] or date_str,
                            time_str=detail_data["time_str"],
                            location=final_location,
                            address=detail_data["address"],
                            prices=detail_data["prices"],
                            requires_booking=detail_data["requires_booking"],
                            phones=detail_data["phones"],
                            tickets_url=tickets_url,
                            buy_ticket_url=detail_data["buy_ticket_url"],
                            image_url=image_url,
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

            # Кнопка создается ТОЛЬКО если есть ссылка на vmuzey.com конкретного события
            reply_markup = None
            if event.buy_ticket_url and "vmuzey.com" in event.buy_ticket_url:
                keyboard = [[InlineKeyboardButton("🎫 Купить билет", url=event.buy_ticket_url)]]
                reply_markup = InlineKeyboardMarkup(keyboard)

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
                                parse_mode=ParseMode.HTML,
                                reply_markup=reply_markup
                            )
                            photo_sent = True
                except Exception as img_err:
                    logger.warning(f"Не удалось загрузить фото для [{event.title}], отправляем текстом: {img_err}")

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
                    logger.error(f"Ошибка отправки текста для [{event.title}]: {e}")
                    continue

            db.mark_as_sent(event.event_id)
            logger.info(f"Успешно отправлено в Telegram: {event.title}")

            await asyncio.sleep(2)

    logger.info("Запуск завершен.")


if __name__ == "__main__":
    asyncio.run(main())
