import asyncio
import logging
import os
import sqlite3
import html
import io
import json
import re
from datetime import datetime, date, timedelta
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
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
    category: str = "museum"
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
    age_rating: str = ""

# Заголовок поста зависит от того, какой это тематический блок недели.
HEADERS_BY_CATEGORY = {
    "museum": "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
    "concerts": "АФИША КОНЦЕРТОВ ТАГАНРОГА",
    "cinema": "АФИША КИНО ТАГАНРОГА",
}

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

def mark_event_sent(url: str):
    """Записывает время последней отправки события. Теперь это просто журнал
    (когда в последний раз постили это событие), а НЕ блокировка повторной
    отправки — событие может законно повторяться неделя за неделей, пока
    его дата не прошла."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sent_events (url) VALUES (?) "
        "ON CONFLICT(url) DO UPDATE SET sent_at = CURRENT_TIMESTAMP",
        (url,)
    )
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

def fix_typography(text: str) -> str:
    """Расставляет неразрывные пробелы по правилам русской типографики,
    чтобы Telegram не переносил строку в неподходящем месте:
    - между инициалами и фамилией ("А. П. Чехова" -> "А. П. Чехова" без
      разрыва между буквами и фамилией);
    - сразу после открывающей кавычки-«ёлочки» (чтобы кавычка не осталась
      одна в конце строки, оторванная от текста в кавычках).
    """
    if not text:
        return text
    # Инициалы + фамилия: "А.П. Чехова" / "А. П. Чехова" -> неразрывные пробелы
    text = re.sub(
        r'([А-ЯЁ]\.)\s*([А-ЯЁ]\.)?\s+([А-ЯЁ][а-яё]+)',
        lambda m: m.group(1) + '\u00A0' + ((m.group(2) + '\u00A0') if m.group(2) else '') + m.group(3),
        text
    )
    # Открывающая кавычка не должна отрываться от следующего слова
    text = text.replace('« ', '«\u00A0')
    return text

def split_address_lines(text: str) -> List[str]:
    """Разбивает строку адреса/места по запятым на отдельные строки —
    каждая часть с новой строки, как просили ('г. Таганрог' отдельно,
    'Библиотека им. Чехова' отдельно и т.д.). Номер дома не отрывается от
    названия улицы: если очередная часть после запятой — просто цифры/дробь
    ('9', '104/1'), она приклеивается обратно к предыдущей строке.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    merged: List[str] = []
    for part in parts:
        if merged and re.fullmatch(r'[\d/]+', part):
            merged[-1] = f"{merged[-1]}, {part}"
        else:
            merged.append(part)

    # Убираем повторяющиеся части (например, "г. Таганрог" на некоторых
    # страницах указан в исходных данных дважды) — без учёта регистра,
    # оставляем первое вхождение.
    seen_lower = set()
    deduped: List[str] = []
    for part in merged:
        key = part.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        deduped.append(part)
    return deduped

def format_event_post(event: Event) -> str:
    lines = []
    header_text = HEADERS_BY_CATEGORY.get(event.category, HEADERS_BY_CATEGORY["museum"])
    lines.append(f"<b>{header_text}</b>")
    
    if event.event_type:
        type_line = event.event_type
        if event.age_rating:
            type_line = f"{type_line} {event.age_rating}"
        lines.append(f"<i>{html.escape(type_line)}</i>")
    
    lines.append(f"<b>{html.escape(fix_typography(event.title))}</b>\n")
    
    if event.date_str:
        lines.append(f"Дата: {html.escape(event.date_str)}")
    if event.time_str:
        lines.append(f"Время: {html.escape(event.time_str)}")
    if event.prices:
        lines.append("Стоимость билета:")
        lines.append(html.escape(event.prices))
        lines.append("")
        
    is_booking_required = event.requires_booking or (event.event_type and "Мастер-класс" in event.event_type)

    if is_booking_required:
        lines.append("<b><i>Предварительная запись обязательна!</i></b>")
        if event.phones:
            lines.append("Телефон для записи:")
            for p in event.phones:
                lines.append(html.escape(p))
            lines.append("")
        else:
            lines.append("")
    elif event.phones:
        # Не мастер-класс и запись не обязательна, но на странице всё равно
        # указан телефон для справок (как у обычных лекций/программ) — показываем.
        lines.append("Телефоны для справок:")
        for p in event.phones:
            lines.append(html.escape(p))
        lines.append("")
    
    if event.location:
        for part in split_address_lines(event.location):
            lines.append(html.escape(fix_typography(part)))
    if event.address:
        lines.append("Адрес:")
        for part in split_address_lines(event.address):
            lines.append(html.escape(fix_typography(part)))
        lines.append("")

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

                # Тип события: берём из вступительной фразы страницы
                # ("...приглашает [N месяца] на ЧТО-ТО...") — дата после
                # "приглашает" необязательна, т.к. не все страницы пишут её
                # сразу (например, у лекций дата иногда упоминается позже).
                # НЕ ищем тип по всему тексту страницы для "экскурсии"/"выставки":
                # в меню сайта на КАЖДОЙ странице есть постоянные пункты
                # "ЭКСКУРСИИ" и "ВЫСТАВКИ", которые иначе ложно совпадают с любым
                # событием, не попавшим в более ранние категории. Остальные типы
                # безопасны для поиска по всему тексту (в меню их нет).
                intro_match = re.search(
                    r'приглаша\S*(?:\s+\d{1,2}\s+[а-яё]+)?\s+на\s+([^.,«]{3,80})',
                    text_content, re.IGNORECASE
                )
                type_source = intro_match.group(1) if intro_match else ""

                if "мастер-класс" in type_source or "мастер класс" in type_source:
                    data["event_type"] = "Мастер-класс"
                elif "литературно-музыкальн" in type_source:
                    data["event_type"] = "Литературно-музыкальная программа"
                elif "музыкальн" in type_source or "вечер" in type_source or "джаз" in type_source:
                    data["event_type"] = "Музыкальная программа"
                elif "лекци" in type_source:
                    data["event_type"] = "Публичная лекция"
                elif "экскурси" in type_source:
                    data["event_type"] = "Экскурсия"
                elif "выставк" in type_source:
                    data["event_type"] = "Выставка"
                elif "мастер-класс" in text_content or "мастер класс" in text_content:
                    data["event_type"] = "Мастер-класс"
                elif "литературно-музыкальн" in text_content:
                    data["event_type"] = "Литературно-музыкальная программа"
                elif "публичную лекцию" in text_content or "публичная лекция" in text_content or "лекци" in text_content:
                    data["event_type"] = "Публичная лекция"
                elif type_source:
                    data["event_type"] = "Музейная программа"
                
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

# ===================== ПАРСИНГ AFISHAGORODA (КОНЦЕРТЫ) =====================

# Общая ссылка на кассу/покупку через виджет — не индивидуальная, при встрече
# исключаем её так же, как исключали общую ссылку на vmuzey.com у музея.
AFISHAGORODA_BASE = "https://tag.afishagoroda.ru"

# Разделы меню сайта — это не события, а категории, ссылки на них выглядят
# точно так же (/events/<slug>), поэтому явно исключаем по названию раздела.
AFISHAGORODA_EXCLUDED_SLUGS = {
    "koncert", "teatr", "show", "muzei", "excursions", "vystavka",
    "detiam", "stand-up", "kvest", "cirk", "sport", "bonus"
}

# Страница бонусной программы сайта — добавляем на неё кликабельную ссылку
# в каждом посте с afishagoroda.ru вместо того, чтобы присылать её как
# отдельное "событие" (ей ошибочно был раньше первый попавшийся URL /events/bonus).
AFISHAGORODA_BONUS_URL = f"{AFISHAGORODA_BASE}/events/bonus"

async def parse_afishagoroda_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
    data = {
        "title": "", "date_str": "", "parsed_date": None, "time_str": "",
        "location": "", "prices": "", "age_rating": "",
        "buy_ticket_url": "", "image_url": None,
    }
    try:
        async with session.get(detail_url, headers=HEADERS, timeout=10) as resp:
            if resp.status != 200:
                return data
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")
            text = soup.get_text(separator=" ")

            # Название события: сначала пробуем <h1> — это заголовок страницы,
            # он есть почти на любом шаблоне сайта. Если его нет — резервный
            # вариант: структурированный блок "Мероприятие: X".
            h1_tag = soup.find("h1")
            if h1_tag:
                data["title"] = h1_tag.get_text(strip=True)
            if not data["title"]:
                title_match = re.search(r'Мероприятие:\s*(.+?)\s*(?=Когда:|$)', text)
                if title_match:
                    data["title"] = title_match.group(1).strip(" .,")

            # Структурированный блок вида:
            # "Когда: 07.09.2026 19:00 Где: г. Таганрог, ... Стоимость билетов: от 2500 до 5500 рублей"
            when_match = re.search(r'Когда:\s*(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})', text)
            if when_match:
                day, month, year = when_match.group(1).split(".")
                try:
                    data["parsed_date"] = date(int(year), int(month), int(day))
                    data["date_str"] = f"{int(day)} {REVERSE_MONTH_MAP.get(int(month), '')}"
                except ValueError:
                    pass
                data["time_str"] = when_match.group(2)

            where_match = re.search(r'Где:\s*(.+?)\s*(?=Стоимость билетов:|Возрастные ограничения:|$)', text)
            if where_match:
                data["location"] = where_match.group(1).strip(" .,")

            price_match = re.search(r'Стоимость билетов:\s*(.+?)\s*(?=Возрастные ограничения:|$)', text)
            if price_match:
                data["prices"] = price_match.group(1).strip(" .,")

            age_match = re.search(r'Возрастные ограничения:\s*(\d{1,2}\+)', text)
            if age_match:
                data["age_rating"] = age_match.group(1)

            img_tag = soup.find("img", src=re.compile(r'/storage/|/media/', re.I))
            if img_tag and img_tag.get("src"):
                data["image_url"] = urljoin(AFISHAGORODA_BASE, img_tag["src"])

            # Индивидуальной ссылки на билет в обычном HTML, скорее всего, нет —
            # кнопка покупки грузится сторонним JS-виджетом (widget.afisha.yandex.ru),
            # который простой парсер не видит. Поэтому ведём кнопку на саму
            # страницу события у них на сайте — там подписчик увидит виджет и купит.
            data["buy_ticket_url"] = detail_url

    except Exception as e:
        logger.error(f"Ошибка при парсинге детальной страницы afishagoroda {detail_url}: {e}")
    return data

async def parse_afishagoroda_concerts(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    seen_urls = set()
    url = f"{AFISHAGORODA_BASE}/events/koncert"

    # Показываем события не дальше чем на 3 месяца вперёд — билеты уже можно
    # купить заранее, но не переспамливаем канал слишком дальними анонсами.
    lookahead_limit = date.today() + timedelta(days=90)

    try:
        async with session.get(url, headers=HEADERS, timeout=12) as resp:
            if resp.status != 200:
                logger.error(f"afishagoroda concert: неожиданный статус {resp.status}")
                return events
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")

            # Ищем все ссылки на отдельные события (/events/<название>), а не
            # гадаем CSS-классы карточек — так надёжнее без реального доступа
            # к разметке сайта. Текст самой ссылки роли не играет — название
            # события берём с детальной страницы (см. ниже), где оно указано
            # в чётком структурированном виде.
            candidate_links = soup.find_all("a", href=re.compile(r'/events/[a-z0-9\-]+/?$', re.I))
            logger.info(f"afishagoroda concert: найдено ссылок-кандидатов: {len(candidate_links)}")

            for a_tag in candidate_links:
                href = ""
                fallback_title = ""
                try:
                    href = a_tag.get("href", "")
                    slug = href.rstrip("/").split("/")[-1].lower()
                    if slug in AFISHAGORODA_EXCLUDED_SLUGS:
                        continue

                    fallback_title = a_tag.get_text(strip=True)

                    event_url = urljoin(AFISHAGORODA_BASE, href)
                    if event_url in seen_urls:
                        continue
                    seen_urls.add(event_url)

                    detail_data = await parse_afishagoroda_detail(session, event_url)

                    # Название берём с детальной страницы (надёжно); если там
                    # почему-то не нашлось — запасной вариант из текста ссылки.
                    title = detail_data.get("title") or fallback_title
                    if not title:
                        continue

                    # Заслон от чужих категорий, которые могут просочиться через
                    # блок "рекомендуем также"/"похожие мероприятия" на странице
                    # концертов — это явно не концерт, судя по названию.
                    non_concert_hints = ("экскурси", "мастер-класс", "выставк", "лекци")
                    if any(hint in title.lower() for hint in non_concert_hints):
                        logger.info(f"Пропуск (похоже, не концерт по названию): {title}")
                        continue

                    parsed_date = detail_data.get("parsed_date")
                    if not parsed_date:
                        # Дату не удалось распознать (например, у событий с
                        # несколькими датами/диапазоном показов формат страницы
                        # другой) — пропускаем, а не показываем без даты и не
                        # пропускаем мимо фильтра на 3 месяца вперёд.
                        logger.info(f"Пропуск (не удалось определить дату): {title} — {event_url}")
                        continue
                    if parsed_date < date.today():
                        logger.info(f"Пропуск (прошедшая дата {parsed_date}): {title}")
                        continue
                    if parsed_date > lookahead_limit:
                        logger.info(f"Пропуск (дальше 3 месяцев вперёд, {parsed_date}): {title}")
                        continue

                    event = Event(
                        title=title,
                        url=event_url,
                        category="concerts",
                        event_type="Концерт",
                        date_str=detail_data.get("date_str", ""),
                        parsed_date=parsed_date,
                        time_str=detail_data.get("time_str", ""),
                        location=detail_data.get("location", ""),
                        prices=detail_data.get("prices", ""),
                        age_rating=detail_data.get("age_rating", ""),
                        hashtags=["#Концерт", "#Таганрог", "#афиша"],
                        buy_ticket_url=detail_data.get("buy_ticket_url", ""),
                        image_url=detail_data.get("image_url"),
                    )
                    events.append(event)
                    logger.info(f"Событие добавлено к отправке: {title} ({parsed_date})")
                except Exception as item_err:
                    logger.error(f"Ошибка при обработке карточки afishagoroda '{fallback_title or href}': {item_err}")
                    continue
    except Exception as e:
        logger.error(f"Ошибка при парсинге afishagoroda (концерты): {e}")
    return events

# ===================== ПАРСИНГ AFISHA.RU (КИНО, ЧАРЛИ МАРМЕЛАД) =====================
# kinocharly.ru отдаёт пустую страницу простому парсеру (сайт на JS), поэтому
# используем afisha.ru как рабочую альтернативу для того же кинотеатра.
CHARLY_BASE = "https://www.afisha.ru"
CHARLY_LISTING_URL = f"{CHARLY_BASE}/taganrog/cinema/"
CHARLY_CINEMA_URL = f"{CHARLY_BASE}/taganrog/cinema/charli-marmelad-taganrog-9584/movie/"

# Данные о расписании на странице не лежат в удобных CSS-классах (они
# хэшированные и могут поменяться при любом редизайне сайта) — вместо этого
# вся структурированная информация зашита в JS-объект window.__nrp прямо
# в HTML. Это гораздо надёжнее для парсинга, чем гадать классы карточек.
NRP_JSON_MARKER = "['root'] = "

# Иногда afisha.ru вместо страницы отдаёт JS-прослойку "Один момент..."
# (похоже на silent-SSO/автологин проверку от Rambler/Сбер ID — она должна
# сама себя перезагрузить через JS, чего простой HTTP-клиент сделать не
# может). На вид это разовая проверка сессии, а не постоянная блокировка,
# поэтому пробуем: 1) подставить куки, которые видели у реальной успешно
# загруженной страницы, 2) сделать "прогревочный" запрос на другую страницу
# сайта, чтобы обзавестись куками сессии, 3) повторить запрос пару раз.
CHARLY_COOKIES = {
    "cookies_privacy_ok": "1",
    "sberloyalty_bait_hidden": "1",
}
CHARLY_HEADERS = {
    **HEADERS,
    "Referer": CHARLY_LISTING_URL,
}

# Блок «Кино» в графике стоит только раз в неделю (по четвергам), поэтому
# вместо сеансов на один день собираем расписание на всю предстоящую
# неделю: сегодня + 6 дней вперёд.
CINEMA_WEEK_DAYS_AHEAD = 6

def _looks_like_charly_interstitial(html_text: str) -> bool:
    """Отличает JS-прослойку "Один момент..." от реальной большой страницы
    с данными — прослойка короткая и содержит характерный заголовок."""
    return len(html_text) < 10000 and "Один момент" in html_text

async def _fetch_charly_day_model(session: aiohttp.ClientSession, url: str, attempt: int = 1) -> Optional[dict]:
    """Загружает страницу репертуара кинотеатра на конкретный день и
    возвращает распарсенный словарь model из window.__nrp (или None при ошибке)."""
    try:
        async with session.get(
            url, headers=CHARLY_HEADERS, cookies=CHARLY_COOKIES, timeout=15
        ) as resp:
            final_url = str(resp.url)
            if resp.status != 200:
                logger.error(f"charly cinema: неожиданный статус {resp.status} для {url} (итоговый URL: {final_url})")
                return None
            html_text = await resp.text()
    except Exception as e:
        logger.error(f"charly cinema: ошибка запроса {url}: {e}")
        return None

    idx = html_text.find(NRP_JSON_MARKER)
    if idx == -1:
        if _looks_like_charly_interstitial(html_text) and attempt < 3:
            logger.info(
                f"charly cinema: похоже на JS-прослойку 'Один момент...' на {url}, "
                f"повторяем попытку {attempt + 1}/3 после паузы"
            )
            await asyncio.sleep(2 * attempt)
            return await _fetch_charly_day_model(session, url, attempt=attempt + 1)

        # Диагностика: логируем куда реально привёл запрос и ПОЛНЫЙ текст
        # ответа (не обрезая) — чтобы можно было прочитать весь JS-скрипт
        # заглушки и понять, воспроизводима ли его логика без браузера.
        full_text = re.sub(r"[ \t]+", " ", html_text).strip()
        logger.error(
            f"charly cinema: не найден блок window.__nrp на {url} после {attempt} попыток. "
            f"Итоговый URL: {final_url}, длина ответа: {len(html_text)} симв.\n"
            f"--- ПОЛНЫЙ ТЕКСТ ОТВЕТА НИЖЕ ---\n{full_text}\n"
            f"--- КОНЕЦ ОТВЕТА ---"
        )
        return None

    # Ищем JSON не через regex до "закрывающей скобки" (вложенных фигурных
    # скобок в объекте слишком много, чтобы поймать регуляркой надёжно),
    # а через инкрементальный разбор с raw_decode — он сам находит конец
    # первого валидного JSON-значения, что бы ни шло дальше в файле.
    start = idx + len(NRP_JSON_MARKER)
    try:
        data, _ = json.JSONDecoder().raw_decode(html_text[start:])
    except Exception as e:
        logger.error(f"charly cinema: не удалось разобрать JSON на {url}: {e}")
        return None

    return data.get("model", {}) or {}

def _format_cinema_date_range(dates: List[date]) -> str:
    """Компактно форматирует список дат показа: одиночная дата, сплошной
    диапазон ("16–19 августа") или перечисление вразнобой ("16, 18, 20 августа")."""
    if not dates:
        return ""
    if len(dates) == 1:
        d = dates[0]
        return f"{d.day} {REVERSE_MONTH_MAP.get(d.month, '')}"

    is_contiguous = all((dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1))
    if is_contiguous:
        first, last = dates[0], dates[-1]
        if first.month == last.month:
            return f"{first.day}–{last.day} {REVERSE_MONTH_MAP.get(last.month, '')}"
        return (
            f"{first.day} {REVERSE_MONTH_MAP.get(first.month, '')} – "
            f"{last.day} {REVERSE_MONTH_MAP.get(last.month, '')}"
        )

    if all(d.month == dates[0].month for d in dates):
        days_list = ", ".join(str(d.day) for d in dates)
        return f"{days_list} {REVERSE_MONTH_MAP.get(dates[0].month, '')}"
    return ", ".join(f"{d.day} {REVERSE_MONTH_MAP.get(d.month, '')}" for d in dates)

async def parse_charly_cinema(session: aiohttp.ClientSession) -> List[Event]:
    """Кинотеатр «Чарли Мармелад»: блок в графике стоит один раз в неделю,
    поэтому вместо сеансов только на день запуска блока собираем расписание
    на всю предстоящую неделю (сегодня + 6 дней) и публикуем один пост на
    фильм с полной раскладкой сеансов по датам.
    """
    events: List[Event] = []
    today = date.today()

    base_model = await _fetch_charly_day_model(session, CHARLY_CINEMA_URL)
    if base_model is None:
        return events

    place_address = (base_model.get("PlaceInfo", {}) or {}).get(
        "AddressWithOptionalCity", "Таганрог, пл. Мира, 7, ТРЦ «Мармелад»"
    )

    available_days = (
        ((base_model.get("Schedule", {}) or {}).get("MovieWidget", {}) or {})
        .get("FilterMenu", {})
        .get("Calendar", {})
        .get("AvailableDays", [])
        or []
    )

    horizon = today + timedelta(days=CINEMA_WEEK_DAYS_AHEAD)
    day_urls = {}
    for d in available_days:
        try:
            day_date = datetime.fromisoformat(d.get("Value", "")).date()
        except Exception:
            continue
        if today <= day_date <= horizon:
            day_urls[day_date] = urljoin(CHARLY_BASE, d.get("Url", ""))
    # На случай если сегодняшний день почему-то не попал в AvailableDays —
    # подстрахуемся: за него данные уже есть в base_model.
    day_urls.setdefault(today, CHARLY_CINEMA_URL)

    # canonical_id фильма -> агрегированные данные с сеансами по датам
    movies: dict = {}

    def process_items(items: list, day_date: date):
        for item in items:
            movie = item.get("Movie", {}) or {}
            sessions = item.get("Sessions", []) or []
            title = (movie.get("Name") or "").strip()
            if not title:
                continue

            schedule_info = movie.get("ScheduleInfo", {}) or {}
            ticket_path = schedule_info.get("Url", "")
            movie_url = urljoin(CHARLY_BASE, movie.get("Url", ""))
            # Movie.Url содержит дату показа на конце (.../16-08-2026/), поэтому
            # для объединения одного и того же фильма за разные дни используем
            # ссылку на билетную страницу (она не зависит от даты), а если её
            # вдруг нет — вырезаем дату из Url вручную.
            canonical_id = ticket_path or re.sub(r"/\d{2}-\d{2}-\d{4}/?$", "/", movie.get("Url", "")) or movie_url

            entry = movies.get(canonical_id)
            if entry is None:
                genres = [
                    g.get("Name", "") for g in (movie.get("Genres", {}) or {}).get("Links", [])
                    if g.get("Name")
                ]
                duration = movie.get("Duration", "")
                type_parts = [", ".join(genres)] if genres else []
                if duration:
                    type_parts.append(duration)
                entry = {
                    "title": title,
                    "url": movie_url,
                    "event_type": "\n".join(p for p in type_parts if p),
                    "age_rating": movie.get("AgeRestriction", ""),
                    "buy_ticket_url": urljoin(CHARLY_BASE, ticket_path) if ticket_path else movie_url,
                    "min_price": schedule_info.get("MinPrice"),
                    "image_url": (movie.get("Poster", {}) or {}).get("Url"),
                    "sessions_by_date": {},
                }
                movies[canonical_id] = entry
            else:
                mp = schedule_info.get("MinPrice")
                if mp is not None and (entry["min_price"] is None or mp < entry["min_price"]):
                    entry["min_price"] = mp

            times = [s.get("Time", "") for s in sessions if s.get("Time")]
            if times:
                entry["sessions_by_date"][day_date] = times

    for day_date, url in sorted(day_urls.items()):
        if day_date == today:
            model = base_model
        else:
            model = await _fetch_charly_day_model(session, url)
            await asyncio.sleep(0.5)
        if model is None:
            logger.warning(f"charly cinema: день {day_date} пропущен (не удалось получить данные)")
            continue
        items = ((model.get("Schedule", {}) or {}).get("MovieWidget", {}) or {}).get("Items", []) or []
        titles_on_day = [(it.get("Movie", {}) or {}).get("Name", "?") for it in items]
        logger.info(f"charly cinema: день {day_date} — найдено фильмов в ответе: {len(items)} ({', '.join(titles_on_day) or 'пусто'})")
        process_items(items, day_date)

    if not movies:
        logger.info("charly cinema: на предстоящую неделю не нашлось фильмов в репертуаре")
        return events

    for entry in movies.values():
        sessions_by_date = entry["sessions_by_date"]
        if not sessions_by_date:
            continue
        sorted_dates = sorted(sessions_by_date.keys())

        date_str = _format_cinema_date_range(sorted_dates)
        time_str = "\n".join(
            f"{d.day} {REVERSE_MONTH_MAP.get(d.month, '')}: {', '.join(sessions_by_date[d])}"
            for d in sorted_dates
        )
        prices = f"от {int(entry['min_price'])} ₽" if entry["min_price"] else ""

        event = Event(
            title=entry["title"],
            url=entry["url"],
            category="cinema",
            event_type=entry["event_type"],
            date_str=date_str,
            parsed_date=sorted_dates[0],
            time_str=time_str,
            location=place_address,
            prices=prices,
            age_rating=entry["age_rating"],
            hashtags=["#Кино", "#Таганрог", "#афиша"],
            buy_ticket_url=entry["buy_ticket_url"],
            image_url=entry["image_url"],
        )
        events.append(event)
        logger.info(f"Фильм добавлен к отправке: {entry['title']} (дней с сеансами: {len(sorted_dates)})")

    return events

# ===================== ОТПРАВКА В TELEGRAM =====================
async def send_event_to_telegram(bot: Bot, user_id: int, event: Event, session: aiohttp.ClientSession):
    text = format_event_post(event)
    
    keyboard = []
    if event.buy_ticket_url:
        keyboard.append([InlineKeyboardButton("Купить билет", url=event.buy_ticket_url)])
    if event.category == "concerts":
        keyboard.append([InlineKeyboardButton("Бонусная программа", url=AFISHAGORODA_BONUS_URL)])
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
# Какой блок публикуется в какой день недели.
# Python: datetime.weekday() -> 0=понедельник ... 6=воскресенье.
# Остальные дни добавляем по мере готовности парсеров.
WEEKDAY_BLOCKS = {
    0: "museum",
    1: "concerts",
    3: "cinema",
}

async def run_block(block_name: str, session: aiohttp.ClientSession) -> List[Event]:
    """Запускает нужный парсер по названию блока."""
    if block_name == "museum":
        return await parse_tgliamz_museums(session)
    elif block_name == "concerts":
        return await parse_afishagoroda_concerts(session)
    elif block_name == "cinema":
        return await parse_charly_cinema(session)
    else:
        logger.error(f"Неизвестный блок: {block_name}")
        return []

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

    # FORCE_BLOCK=museum (или concerts, и т.д.) запускает конкретный блок
    # вручную, независимо от дня недели — удобно для тестирования.
    force_block = os.environ.get("FORCE_BLOCK", "").strip().lower()
    today_weekday = date.today().weekday()
    block_name = force_block or WEEKDAY_BLOCKS.get(today_weekday)

    if not block_name:
        logger.info(f"Сегодня (день недели {today_weekday}) не назначен ни один блок — пропускаем.")
        return

    logger.info(f"Запускаем блок: {block_name}")

    init_db()
    bot = Bot(token=token)

    async with aiohttp.ClientSession() as session:
        logger.info("Начинаем сбор событий...")
        events = await run_block(block_name, session)
        
        # Строгая хронологическая сортировка (от ранних к поздним)
        events.sort(key=lambda x: (x.parsed_date or date.max, x.time_str))
        
        logger.info(f"Найдено событий для отправки: {len(events)}")

        for event in events:
            await send_event_to_telegram(bot, user_id, event, session)

if __name__ == "__main__":
    asyncio.run(main())
