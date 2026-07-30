import asyncio
import html
import io
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
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

# ===================== КОНСТАНТЫ И НАСТРОЙКИ =====================
# Исключаем общий номер ТГЛИАМЗ (8 (8634) 61-00-13) и служебные номера
EXCLUDED_PHONES = {"88634610013", "78634610013", "8634610013", "383496", "38-34-96"}

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры",
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
]

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

MUSEUM_BRANCHES = [
    {
        "keys": ["литературный музей", "литературно-музыкальн"],
        "name": "Литературный музей А.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЛитературныйМузейЧехова"
    },
    {
        "keys": ["юрнкц", "южно-российский"],
        "name": "ЮРНКЦ А.П. Чехова",
        "address": "ул. Октябрьская, 9",
        "tag": "#ЮРНКЦЧехова"
    },
    {
        "keys": ["дворец алфераки", "историко-краеведческий"],
        "name": "Историко-краеведческий музей (Дворец Алфераки)",
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
    date_str: str = ""
    time_str: str = ""
    location: str = ""
    address: str = ""
    description: str = ""
    prices: str = ""
    phones: List[tuple] = field(default_factory=list)  # (display, tel_href)
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


def clean_and_format_phone(raw_phone: str) -> Optional[tuple]:
    """Форматирует телефон в (экранный_вид, tel_ссылка) и отсеивает общий номер ТГЛИАМЗ."""
    digits = re.sub(r"\D", "", raw_phone)
    if not digits:
        return None

    # Проверка черного списка номеров
    if any(ex in digits for ex in EXCLUDED_PHONES):
        return None

    if len(digits) == 6:
        display = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
        tel = f"+78634{digits}"
        return (display, tel)
    elif len(digits) == 11:
        if digits[1] == '9':
            display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
            tel = f"+7{digits[1:]}"
        elif digits[1:5] == '8634':
            display = f"8 (8634) {digits[5:7]}-{digits[7:9]}-{digits[9:]}"
            tel = f"+7{digits[1:]}"
        else:
            display = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
            tel = f"+7{digits[1:]}"
        return (display, tel)

    return None


def extract_all_phones(html_or_text: str) -> List[tuple]:
    """Собирает телефоны со всей страницы, включая футер."""
    formatted_phones = []
    seen_digits = set()

    soup = BeautifulSoup(html_or_text, "html.parser")
    # 1. Ссылки tel:
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("tel:"):
            res = clean_and_format_phone(a["href"])
            if res:
                digits = re.sub(r"\D", "", res[1])
                if digits not in seen_digits:
                    seen_digits.add(digits)
                    formatted_phones.append(res)

    # 2. Поиск по тексту
    pattern = r"(?:\+?7|8)[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}|\b\d{2}-\d{2}-\d{2}\b"
    matches = re.findall(pattern, html_or_text)
    for m in matches:
        res = clean_and_format_phone(m)
        if res:
            digits = re.sub(r"\D", "", res[1])
            if digits not in seen_digits:
                seen_digits.add(digits)
                formatted_phones.append(res)

    return formatted_phones


def is_event_past(date_str: str) -> bool:
    """Фильтрует мероприятия, дата проведения которых уже прошла."""
    if not date_str:
        return False

    try:
        now = datetime.now()
        # Ищем дату формата "19 июля" или "19 июля 2026"
        match = re.search(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", date_str.lower())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            year = int(match.group(3)) if match.group(3) else now.year

            if month_str in MONTHS_RU:
                month = MONTHS_RU[month_str]
                event_date = datetime(year, month, day, 23, 59)
                
                # Если событие раньше сегодняшнего дня — оно прошло
                if event_date < now:
                    return True
    except Exception as e:
        logger.debug(f"Ошибка проверки даты '{date_str}': {e}")

    return False


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
    date_str = html.escape(event.date_str.strip())
    time_str = html.escape(event.time_str.strip())
    location = html.escape(event.location.strip())
    address = html.escape(event.address.strip())
    description = event.description.strip()
    prices = html.escape(event.prices.strip())

    lines = []

    if event.category == Category.THEATRE_MONTH:
        lines.append("<b>ТАГАНРОГСКИЙ ТЕАТР ИМ. А.П. ЧЕХОВА</b>")
        lines.append("<i>Репертуар и анонс спектаклей</i>\n")
    elif event.category == Category.MUSEUM:
        lines.append("<b>МУЗЕИ И ВЫСТАВКИ ТАГАНРОГА</b>")
        lines.append("<i>Таганрогский музей-заповедник</i>\n")

    lines.append(f"<b>{title}</b>\n")

    if description:
        lines.append(f"{description}\n")

    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")

    if prices:
        lines.append(f"<b>Стоимость билета:</b> {prices}")

    if location:
        lines.append(f"\n{location}")
    if address:
        lines.append(f"{address}.")

    # Динамическая конкретизированная плашка телефонов
    if event.phones:
        text_full = (title + " " + description).lower()
        if any(k in text_full for k in ["мастер-класс", "занятие", "экскурсия", "запись", "лекция"]):
            lines.append("\n📞 <b>Справки и запись на мероприятие:</b>")
        else:
            lines.append("\n📞 <b>Справки и подробности по телефонам:</b>")

        for disp, tel in event.phones:
            lines.append(f"<a href='tel:{tel}'>{disp}</a>")

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

                        # Игнорируем спектакли, которые уже прошли
                        if is_event_past(date_str):
                            continue

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
                                phones=extract_all_phones("+7 (8634) 38-29-68"),
                                tickets_url=tickets_url,
                                buy_ticket_url=tickets_url,
                                image_url=image_url,
                                tags=["#Таганрог", "#ТеатрЧехова", "#афиша"]
                            )
                        )
    except Exception as e:
        logger.error(f"Ошибка парсинга Театра Чехова: {e}")

    return events


async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "description": "", 
        "date_str": "", 
        "time_str": "", 
        "location": "", 
        "address": "", 
        "prices": "", 
        "phones": [], 
        "branch_tag": "",
        "buy_ticket_url": "",
        "is_shop": False,
        "is_past": False
    }
    try:
        async with session.get(detail_url, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()

                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data

                soup = BeautifulSoup(html_text, "html.parser")

                # Извлекаем ВСЕ контакты со страницы
                data["phones"] = extract_all_phones(html_text)

                # Поиск кнопки покупки билетов
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    link_text = a_tag.get_text(strip=True).lower()

                    if "vmuzey.com" in href or "купить билет" in link_text:
                        if href.startswith("http"):
                            data["buy_ticket_url"] = href
                            break

                # Извлекаем описание
                content_block = soup.select_one(
                    ".detail-text, .news-detail, .content-text, .detail_text, "
                    ".workarea, .content, article, .event-detail, .page-content"
                )
                page_full_text = soup.get_text()

                if content_block:
                    for s in content_block(["script", "style", "nav", "footer"]):
                        s.extract()

                    paragraphs = []
                    important_notes = []

                    for el in content_block.find_all(["p", "div", "li"]):
                        txt = el.get_text(strip=True)
                        if len(txt) < 10 or txt.lower().startswith("купить билет"):
                            continue

                        if any(phrase in txt.lower() for phrase in [
                            "предварительная запись", "количество мест ограничено", 
                            "приглашаются", "справки по телефону", "запись по телефону",
                            "материалы предоставляются", "по предварительной заявке"
                        ]):
                            if txt not in important_notes:
                                important_notes.append(html.escape(txt))
                        else:
                            if txt not in paragraphs and not txt.startswith("Тел"):
                                paragraphs.append(html.escape(txt))

                    desc_parts = []
                    if paragraphs:
                        desc_parts.append("\n\n".join(paragraphs[:3]))
                    if important_notes:
                        desc_parts.append("📌 <b>Важно:</b>\n" + "\n".join([f"• {note}" for note in important_notes]))

                    data["description"] = "\n\n".join(desc_parts)

                # Определение подразделения музея
                text_to_check = page_full_text.lower()
                for branch in MUSEUM_BRANCHES:
                    if any(k in text_to_check for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # Даты и время
                date_match = re.search(
                    r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", 
                    page_full_text, re.I
                )
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()
                    if is_event_past(data["date_str"]):
                        data["is_past"] = True

                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", page_full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*|цена[^\d]*?)(\d+\s*руб[а-я]*)", page_full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1)

    except Exception as e:
        logger.warning(f"Ошибка получения деталей страницы {detail_url}: {e}")
    return data


async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    base_url = "https://tgliamz.ru"

    target_urls = [
        "https://tgliamz.ru/calendar/",
        "https://tgliamz.ru/afisha/",
        "https://tgliamz.ru/news/"
    ]

    candidate_urls = set()

    for start_url in target_urls:
        try:
            async with session.get(start_url, timeout=15) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, "html.parser")

                    for a in soup.find_all("a", href=True):
                        href = a["href"].strip()
                        if "ELEMENT_ID=" in href or any(part in href for part in ["/calendar/", "/news/", "/afisha/"]):
                            if href not in ["/calendar/", "/news/", "/afisha/", "/"]:
                                full_url = urljoin(base_url, href)
                                candidate_urls.add(full_url)
        except Exception as e:
            logger.error(f"Ошибка при сборе ссылок с {start_url}: {e}")

    logger.info(f"Найдено карточек ТГЛИАМЗ для обработки: {len(candidate_urls)}")

    for event_url in candidate_urls:
        detail_data = await parse_tgliamz_detail(session, event_url)

        # Отсекаем сувениры и устаревшие события
        if detail_data["is_shop"] or detail_data["is_past"]:
            continue

        try:
            async with session.get(event_url, timeout=10) as page_resp:
                if page_resp.status == 200:
                    page_html = await page_resp.text()
                    p_soup = BeautifulSoup(page_html, "html.parser")

                    h1 = p_soup.select_one("h1, .page-header, .news-detail-title, .title")
                    title = h1.get_text(strip=True) if h1 else ""

                    if not title or len(title) < 3 or "ошибка" in title.lower():
                        continue

                    img_el = p_soup.select_one(".detail-picture, .news-detail img, .content img, article img, .page-content img")
                    image_url = None
                    if img_el and img_el.get("src"):
                        image_url = urljoin(base_url, img_el["src"])

                    event_id = f"tgliamz_{hash(event_url)}"
                    final_location = detail_data["location"] or "Таганрогский музей-заповедник"
                    final_tags = generate_museum_tags(
                        title + " " + detail_data["description"], 
                        detail_data["branch_tag"]
                    )

                    events.append(
                        Event(
                            event_id=event_id,
                            category=Category.MUSEUM,
                            title=title,
                            date_str=detail_data["date_str"],
                            time_str=detail_data["time_str"],
                            location=final_location,
                            address=detail_data["address"],
                            description=detail_data["description"],
                            prices=detail_data["prices"],
                            phones=detail_data["phones"],
                            tickets_url=event_url,
                            buy_ticket_url=detail_data["buy_ticket_url"],
                            image_url=image_url,
                            tags=final_tags
                        )
                    )
        except Exception as e:
            logger.warning(f"Ошибка обработки страницы события {event_url}: {e}")

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
        logger.info("Запуск парсинга...")
        events = await fetch_events(session)
        logger.info(f"Всего актуальных мероприятий найдено: {len(events)}")

        for event in events:
            if db.is_sent(event.event_id):
                logger.info(f"Событие [{event.title}] уже было отправлено, пропускаем.")
                continue

            caption = format_caption(event)
            photo_sent = False

            # Простая понятная кнопка "Купить билет" без лишних упоминаний
            reply_markup = None
            if event.buy_ticket_url:
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎫 Купить билет", url=event.buy_ticket_url)]]
                )

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
