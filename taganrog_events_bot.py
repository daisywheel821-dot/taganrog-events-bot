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

# ===================== КОНФИГУРАЦИЯ И КОНСТАНТЫ =====================
DB_PATH = "data/taganrog_events.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

STRICT_SOUVENIR_WORDS = [
    "сувенирная продукция", "купить сувенир", "в продаже сувениры", 
    "музейный магазин", "прейскурант цен на товары", "каталог сувениров"
]

EXCLUDED_PHONE_DIGITS = "8634610013"

# Общая ссылка "Купить билет", присутствующая в шапке/футере КАЖДОЙ страницы сайта.
# Её нельзя считать индивидуальной ссылкой на билет конкретного события.
GENERIC_TICKET_URL = "https://vmuzey.com/museum/taganrogskiy-muzey-zapovednik"

# Заголовки карточек-модулей, которые не являются отдельными событиями
# (например, сводный дайджест "Афиша выходного дня", который всегда идёт первым в списке).
EXCLUDED_CARD_TITLES = ["афиша выходного дня"]

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}
REVERSE_MONTH_MAP = {v: k for k, v in MONTH_MAP.items()}

MUSEUM_BRANCHES = [
    {"keys": ["литературный музей", "литературно-музыкальн"], "name": "Литературный музей А.П. Чехова", "address": "ул. Октябрьская, 9", "tag": "#ЛитературныйМузей"},
    {"keys": ["юрнкц", "южно-российский"], "name": "ЮРНКЦ А.П. Чехова", "address": "ул. Октябрьская, 9", "tag": "#ЮРНКЦЧехова"},
    {"keys": ["дворец алфераки", "историко-краеведческ"], "name": "Историко-краеведческий музей (Дворец Алфераки)", "address": "ул. Фрунзе, 41", "tag": "#ДворецАлфераки"},
    {"keys": ["домик чехова"], "name": "Музей «Домик Чехова»", "address": "ул. Чехова, 69", "tag": "#ДомикЧехова"},
    {"keys": ["лавка чеховых", "лавка чехова"], "name": "Музей «Лавка Чеховых»", "address": "ул. Александровская, 100", "tag": "#ЛавкаЧеховых"},
    {"keys": ["музей василенко", "василенко"], "name": "Музей И.Д. Василенко", "address": "ул. Чехова, 88", "tag": "#МузейВасиленко"},
    {"keys": ["музей дурова", "дурова"], "name": "Музей А.А. Дурова", "address": "ул. А. Глушко, 44", "tag": "#МузейДурова"},
    {"keys": ["градостроительства"], "name": "Музей градостроительства и быта", "address": "ул. Фрунзе, 80", "tag": "#МузейГрадостроительства"}
]

@dataclass
class Event:
    title: str
    url: str
    event_type: str = ""
    date_str: str = ""
    parsed_date: Optional[date] = None
    time_str: str = ""
    location: str = ""
    address: str = ""
    prices: str = ""
    requires_booking: bool = False
    phones: List[str] = field(default_factory=list)
    branch_tag: str = ""
    hashtags: List[str] = field(default_factory=list)
    buy_ticket_url: str = ""
    image_url: Optional[str] = None

# ===================== БАЗА ДАННЫХ SQLite =====================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_events (
            url TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_event_sent(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_events WHERE url = ?", (url,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_event_sent(url: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_events (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_souvenir_shop_item(html_text: str) -> bool:
    text_lower = html_text.lower()
    return any(word in text_lower for word in STRICT_SOUVENIR_WORDS)

def parse_event_date(date_text: str) -> Optional[date]:
    if not date_text:
        return None
    try:
        match = re.search(r'(\d{1,2})\s+([а-яА-Я]+)', date_text.lower())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            month = MONTH_MAP.get(month_str)
            if month:
                current_year = date.today().year
                event_date = date(current_year, month, day)
                if event_date < date.today() and date.today().month == 12 and month == 1:
                    event_date = date(current_year + 1, month, day)
                return event_date
    except Exception as e:
        logger.warning(f"Не удалось распарсить дату '{date_text}': {e}")
    return None

def extract_targeted_phones(text: str) -> List[str]:
    """Извлекает номера для записи/брони и приводит их к читаемому виду.

    Сужаем поиск до фрагментов рядом со словами "телефон/тел./справки/
    бронирование" — это снижает риск зацепить случайный номер не по теме.
    Берём окна вокруг КАЖДОГО такого упоминания на странице (не только
    первого), чтобы не промахнуться, если слово встречается несколько раз.
    Если ни одного такого слова нет — ищем по всему тексту (подстраховка).
    """
    keyword_matches = list(re.finditer(r'(?:телефон|тел\.?|справки|бронирование)', text, re.IGNORECASE))
    if keyword_matches:
        search_scope = " ".join(text[m.end():m.end() + 300] for m in keyword_matches)
    else:
        search_scope = text

    raw_phones = re.findall(
        r'(?:\+?7|8)?[\s(-]*\d{3,4}[\s)-]*\d{2,3}[\s-]*\d{2}[\s-]*\d{2}',
        search_scope
    )

    valid_phones = []
    seen_digits = set()
    for p in raw_phones:
        digits = re.sub(r'\D', '', p)
        if len(digits) < 10 or EXCLUDED_PHONE_DIGITS in digits:
            continue
        if digits in seen_digits:
            continue
        seen_digits.add(digits)

        # Нормализуем к 10 значащим цифрам без кода страны/восьмёрки
        if len(digits) == 11:
            core = digits[1:]
        else:
            core = digits[-10:]

        if core[0] == "9":
            display = f"+7 ({core[0:3]}) {core[3:6]}-{core[6:8]}-{core[8:10]}"
        else:
            display = f"8 ({core[0:4]}) {core[4:6]}-{core[6:8]}-{core[8:10]}"
        valid_phones.append(display)

    return valid_phones

def extract_hashtags(text: str) -> List[str]:
    """Извлекает реальные хештеги, указанные на странице события (например,
    '#ТГЛИАМЗ #ЮРНКЦЧехова #творчество #Таганрог #афиша'), сохраняя порядок
    появления и убирая дубликаты."""
    found = re.findall(r'#[A-Za-zА-Яа-яЁё0-9_]+', text)
    return list(dict.fromkeys(found))

def format_event_post(event: Event) -> str:
    lines = []
    lines.append("<b>МУЗЕЙНАЯ АФИША ТАГАНРОГА</b>")
    
    if event.event_type:
        lines.append(f"<i>{html.escape(event.event_type)}</i>")
    
    lines.append(f"<b>{html.escape(event.title)}</b>\n")
    
    if event.date_str:
        lines.append(f"Дата: {html.escape(event.date_str)}")
    if event.time_str:
        lines.append(f"Время: {html.escape(event.time_str)}")
    if event.prices:
        lines.append(f"Стоимость билета: {html.escape(event.prices)}\n")
        
    if event.requires_booking or (event.event_type and "Мастер-класс" in event.event_type):
        lines.append("<b><i>Предварительная запись обязательна!</i></b>")
        if event.phones:
            phones_str = ", ".join([html.escape(p) for p in event.phones])
            lines.append(f"Телефон для записи: {phones_str}\n")
        else:
            lines.append("")
    
    if event.location:
        lines.append(f"{html.escape(event.location)}")
    if event.address:
        lines.append(f"Адрес: {html.escape(event.address)}\n")

    # Хештеги: используем реальные, указанные на странице события.
    # Если на странице их не нашли — подстраховка старыми общими тегами,
    # чтобы пост не остался вовсе без хештегов.
    if event.hashtags:
        tags = [html.escape(t) for t in event.hashtags]
    else:
        tags = []
        if event.branch_tag:
            tags.append(html.escape(event.branch_tag))
        tags.extend(["#Таганрог", "#АфишаТаганрог"])
    lines.append(" ".join(tags))
        
    return "\n".join(lines)

# ===================== ПАРСИНГ TGLIAMZ =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "event_type": "", "date_str": "", "parsed_date": None,
        "time_str": "", "location": "", "address": "", "prices": "",
        "requires_booking": False, "phones": [], "branch_tag": "",
        "hashtags": [], "buy_ticket_url": "", "image_url": None, "is_shop": False
    }
    try:
        async with session.get(detail_url, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data
                
                soup = BeautifulSoup(html_text, "html.parser")

                # Видимый текст страницы (с сохранением пробелов) — используем его,
                # а не сырой html_text, для извлечения телефонов и хештегов,
                # чтобы не терять форматирование (пробелы, скобки, дефисы).
                raw_text = soup.get_text(separator=" ")
                text_content = raw_text.lower()

                # Типизация
                if "мастер-класс" in text_content or "мастер класс" in text_content:
                    data["event_type"] = "Мастер-класс"
                elif "литературно-музыкальн" in text_content or "джаз" in text_content:
                    data["event_type"] = "Литературно-музыкальная программа"
                elif "экскурси" in text_content:
                    data["event_type"] = "Экскурсия"
                elif "выставк" in text_content:
                    data["event_type"] = "Выставка"
                
                # Бронь и телефоны
                if "предварительная запись" in text_content or "запись по телефону" in text_content:
                    data["requires_booking"] = True
                data["phones"] = extract_targeted_phones(raw_text)

                # Реальные хештеги, указанные на странице события
                data["hashtags"] = extract_hashtags(raw_text)
                
                # Поиск филиала
                for branch in MUSEUM_BRANCHES:
                    if any(key in text_content for key in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break
                
                # Билеты: сначала ищем ссылку по паттерну ИНДИВИДУАЛЬНОЙ страницы
                # события у vmuzey (например vmuzey.com/event/... или /product/...) —
                # такая ссылка точно относится к конкретному мероприятию.
                #
                # Важно: ссылки на сайте встречаются и с полным протоколом
                # ("https://vmuzey.com/..."), и в protocol-relative виде
                # ("//vmuzey.com/..."). Достраиваем протокол через urljoin и
                # сравниваем БЕЗ схемы, иначе общая ссылка в укороченном виде
                # проходит проверку как "индивидуальная" и ломает кнопку в Telegram
                # (ошибка "url host is empty").
                def _strip_scheme(u: str) -> str:
                    return re.sub(r'^https?:', '', u).rstrip('/')

                generic_normalized = _strip_scheme(GENERIC_TICKET_URL)

                individual_link = soup.find("a", href=re.compile(r'vmuzey\.com/(event|product)/', re.I))
                if individual_link and individual_link.get("href"):
                    data["buy_ticket_url"] = urljoin("https://tgliamz.ru", individual_link["href"].strip())
                else:
                    # Иначе берём любую ссылку из блока, НЕ совпадающую с общей
                    # ссылкой "Купить билет" из шапки/футера сайта (она есть на
                    # каждой странице и не является индивидуальной).
                    buy_candidates = soup.find_all("a", href=re.compile(r'vmuzey|afisha|tickets', re.I))
                    for candidate in buy_candidates:
                        raw_href = (candidate.get("href") or "").strip()
                        if not raw_href:
                            continue
                        full_href = urljoin("https://tgliamz.ru", raw_href)
                        if _strip_scheme(full_href) != generic_normalized:
                            data["buy_ticket_url"] = full_href
                            break

                img_tag = soup.find("img", src=re.compile(r'/upload/'))
                if img_tag and img_tag.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_tag["src"])
                    
                # Дата и время указаны в свободном тексте БЕЗ меток "Дата:"/"Время:",
                # например: "Суббота, 8 августа в 15.00" или "16 августа в 18.00".
                # Сначала ищем связку дата+время в одном месте (так на сайте обычно
                # оформлена ключевая строка события), при неудаче — только дату.
                combo_match = re.search(
                    r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+в\s+(\d{1,2})[.:](\d{2})',
                    text_content, re.IGNORECASE
                )
                if combo_match:
                    data["date_str"] = f"{combo_match.group(1)} {combo_match.group(2)}".title()
                    data["parsed_date"] = parse_event_date(data["date_str"])
                    data["time_str"] = f"{combo_match.group(3)}:{combo_match.group(4)}"
                else:
                    date_only_match = re.search(
                        r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)',
                        text_content, re.IGNORECASE
                    )
                    if date_only_match:
                        data["date_str"] = f"{date_only_match.group(1)} {date_only_match.group(2)}".title()
                        data["parsed_date"] = parse_event_date(data["date_str"])

                    time_only_match = re.search(r'\bв\s*(\d{1,2})[.:](\d{2})\b', text_content)
                    if time_only_match:
                        data["time_str"] = f"{time_only_match.group(1)}:{time_only_match.group(2)}"
                    
                price_match = re.search(r'(?i)(?:цена|стоимость|билет)[^0-9]*(\d{2,4})\s*(?:руб|р)', text_content)
                if price_match:
                    data["prices"] = f"{price_match.group(1)} руб. (в кассе музея)"

    except Exception as e:
        logger.error(f"Ошибка при парсинге деталей {detail_url}: {e}")
    return data

async def parse_tgliamz_museums(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    seen_urls = set()  # защита от дублей внутри одного прогона
    url = "https://tgliamz.ru/calendar/"
    base_url = "https://tgliamz.ru"
    try:
        async with session.get(url, headers=HEADERS, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                soup = BeautifulSoup(html_text, "html.parser")
                
                items = soup.find_all("div", class_=re.compile(r'event|calendar-item|afisha|news-item', re.I))
                logger.info(f"Найдено блоков афиши на странице: {len(items)}")
                
                for item in items:
                    try:
                        title = ""
                        event_url = ""
                        
                        for a_tag in item.find_all("a", href=True):
                            text = a_tag.get_text(strip=True)
                            if text: 
                                title = text
                                event_url = urljoin(base_url, a_tag["href"])
                                break
                                
                        if not title:
                            continue
                        if is_event_sent(event_url):
                            logger.info(f"Пропуск (уже было отправлено ранее): {title} — {event_url}")
                            continue

                        # Пропускаем дубли одного и того же события внутри текущего прогона
                        # (возникают из-за вложенных div-блоков с похожими классами)
                        if event_url in seen_urls:
                            logger.info(f"Пропуск (дубль в текущем прогоне): {title}")
                            continue

                        # Пропускаем сводный модуль "Афиша выходного дня" — это не
                        # отдельное событие, а дайджест-баннер, который сайт всегда
                        # ставит первым в списке.
                        if any(excluded in title.lower() for excluded in EXCLUDED_CARD_TITLES):
                            logger.info(f"Пропуск (исключённая карточка-модуль): {title}")
                            continue

                        seen_urls.add(event_url)

                        # Дата прямо из карточки в списке афиши (формат ДД.ММ.ГГГГ) —
                        # надёжный источник, не зависящий от формулировок текста на
                        # детальной странице. Используем её в приоритете для фильтрации.
                        list_date = None
                        list_date_match = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', item.get_text())
                        if list_date_match:
                            try:
                                list_date = date(
                                    int(list_date_match.group(3)),
                                    int(list_date_match.group(2)),
                                    int(list_date_match.group(1))
                                )
                            except ValueError:
                                list_date = None

                        detail_data = await parse_tgliamz_detail(session, event_url)
                        
                        if detail_data.get("is_shop"):
                            logger.info(f"Пропуск (страница сувенирной продукции): {title}")
                            continue

                        # Итоговая дата события: приоритет — дата из списка афиши,
                        # при её отсутствии — дата, найденная в тексте детальной страницы.
                        final_parsed_date = list_date or detail_data.get("parsed_date")

                        # Отсеиваем прошедшие события
                        if final_parsed_date and final_parsed_date < date.today():
                            logger.info(f"Пропуск (прошедшая дата {final_parsed_date}): {title}")
                            continue

                        # Текст даты для поста: берём из детальной страницы (там есть
                        # человекочитаемая формулировка "8 августа"); если её не нашли,
                        # но дата из списка есть — формируем строку из неё же.
                        final_date_str = detail_data.get("date_str") or (
                            f"{final_parsed_date.day} {REVERSE_MONTH_MAP.get(final_parsed_date.month, '')}"
                            if final_parsed_date else ""
                        )

                        event = Event(
                            title=title,
                            url=event_url,
                            event_type=detail_data.get("event_type", ""),
                            date_str=final_date_str,
                            parsed_date=final_parsed_date,
                            time_str=detail_data.get("time_str", ""),
                            location=detail_data.get("location", ""),
                            address=detail_data.get("address", ""),
                            prices=detail_data.get("prices", ""),
                            requires_booking=detail_data.get("requires_booking", False),
                            phones=detail_data.get("phones", []),
                            branch_tag=detail_data.get("branch_tag", ""),
                            hashtags=detail_data.get("hashtags", []),
                            buy_ticket_url=detail_data.get("buy_ticket_url", ""),
                            image_url=detail_data.get("image_url")
                        )
                        events.append(event)
                        logger.info(f"Событие добавлено к отправке: {title} ({final_parsed_date})")
                    except Exception as item_err:
                        logger.error(f"Ошибка при обработке карточки '{title or event_url}': {item_err}")
                        continue
    except Exception as e:
        logger.error(f"Ошибка при парсинге календаря TGLIAMZ: {e}")
    return events

# ===================== ОТПРАВКА В TELEGRAM =====================
async def send_event_to_telegram(bot: Bot, user_id: int, event: Event, session: aiohttp.ClientSession):
    text = format_event_post(event)
    
    keyboard = []
    if event.buy_ticket_url:
        keyboard.append([InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    try:
        photo_sent = False
        if event.image_url:
            try:
                async with session.get(event.image_url, headers=HEADERS, timeout=10) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=io.BytesIO(image_bytes),
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                        photo_sent = True
            except Exception as img_err:
                logger.warning(f"Не удалось загрузить фото для '{event.title}', отправляем текстом: {img_err}")

        if not photo_sent:
            await bot.send_message(
                chat_id=user_id, 
                text=text, 
                parse_mode=ParseMode.HTML, 
                reply_markup=reply_markup
            )
            
        mark_event_sent(event.url)
        logger.info(f"Успешно отправлено событие: {event.title}")
        await asyncio.sleep(1.5)
        
    except TelegramError as e:
        logger.error(f"Ошибка Telegram при отправке '{event.title}': {e}")

# ===================== ОСНОВНАЯ ТОЧКА ВХОДА =====================
async def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    user_id_str = os.environ.get("TELEGRAM_USER_ID") or os.getenv("CHAT_ID")

    if not token or not user_id_str:
        logger.error("Ошибка: Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_USER_ID в переменных окружения.")
        return

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.error("Ошибка: TELEGRAM_USER_ID должен быть числовым значением.")
        return

    init_db()
    bot = Bot(token=token)

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await parse_tgliamz_museums(session)
        
        # Строгая хронологическая сортировка (от ранних к поздним)
        events.sort(key=lambda x: (x.parsed_date or date.max, x.time_str))
        
        logger.info(f"Найдено событий для отправки: {len(events)}")

        for event in events:
            await send_event_to_telegram(bot, user_id, event, session)

if __name__ == "__main__":
    asyncio.run(main())
