import html
import logging
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Общий номер музея-заповедника, который нужно исключать из вывода
EXCLUDED_PHONES = {"88634610013", "78634610013", "+78634610013"}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


def clean_phone_number(phone_raw: str) -> Optional[str]:
    """Очищает телефонный номер и проверяет его на вхождение в черный список."""
    digits_only = re.sub(r"\D", "", phone_raw)
    
    # Приводим к стандарту 8634XXXXXX
    if digits_only.startswith("7") or digits_only.startswith("8"):
        digits_only = digits_only[1:]
        
    # Если это общий номер ТГЛИАМЗ (61-00-13) — игнорируем его
    if "8634610013" in digits_only or digits_only in EXCLUDED_PHONES:
        return None
        
    return phone_raw.strip()


def extract_all_phones(html_or_text: str) -> List[str]:
    """Извлекает все кликабельные и текстовые телефоны со всей страницы (включая футер)."""
    phones = []
    
    # 1. Ищем ссылки tel:
    soup = BeautifulSoup(html_or_text, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("tel:"):
            raw_phone = a["href"].replace("tel:", "").strip()
            cleaned = clean_phone_number(raw_phone)
            if cleaned and cleaned not in phones:
                phones.append(cleaned)
                
    # 2. Ищем регулярным выражением по всему тексту
    pattern = r"(?:\+7|8)[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}"
    matches = re.findall(pattern, html_or_text)
    for m in matches:
        cleaned = clean_phone_number(m)
        if cleaned and cleaned not in phones:
            phones.append(cleaned)
            
    return phones


def is_event_past(date_str: str) -> bool:
    """Проверяет, не прошло ли событие по дате."""
    if not date_str:
        return False
        
    try:
        now = datetime.now()
        # Ищем день и месяц в строке даты
        match = re.search(r"(\d{1,2})\s+([а-я]+)", date_str.lower())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            
            if month_str in MONTHS_RU:
                month = MONTHS_RU[month_str]
                year = now.year
                
                # Если месяц события уже прошёл в этом году, возможно речь про следующий год
                event_date = datetime(year, month, day, 23, 59)
                
                # Если событие раньше текущего дня — оно прошло
                if event_date < now:
                    return True
    except Exception as e:
        logger.debug(f"Не удалось распарсить дату '{date_str}': {e}")
        
    return False


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

                # Извлекаем ВСЕ телефоны со всей страницы (включая подвал сайта)
                data["phones"] = extract_all_phones(html_text)

                # 1. Ссылка на покупку билета (без привязки к Пушкинской карте)
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    link_text = a_tag.get_text(strip=True).lower()
                    
                    if "vmuzey.com" in href or "купить билет" in link_text:
                        if href.startswith("http"):
                            data["buy_ticket_url"] = href
                            break

                # 2. Извлечение основного текста и примечаний
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

                # 3. Определение филиала
                text_to_check = page_full_text.lower()
                for branch in MUSEUM_BRANCHES:
                    if any(k in text_to_check for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # 4. Извлечение даты и времени + Проверка на актуальность
                date_match = re.search(
                    r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", 
                    page_full_text, re.I
                )
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()
                    # Проверяем, не прошло ли событие
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
    
    # Сканируем ключевые разделы сайта
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
                        if any(part in href for part in ["/calendar/", "/news/", "/afisha/", "ELEMENT_ID="]):
                            if href not in ["/calendar/", "/news/", "/afisha/"]:
                                full_url = urljoin(base_url, href)
                                candidate_urls.add(full_url)
        except Exception as e:
            logger.error(f"Ошибка при сборе ссылок с {start_url}: {e}")

    for event_url in candidate_urls:
        detail_data = await parse_tgliamz_detail(session, event_url)
        
        # Фильтруем магазины и ПРОШЕДШИЕ события
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

    # Удаляем дубликаты
    unique_events = {}
    for ev in events:
        if ev.event_id not in unique_events:
            unique_events[ev.event_id] = ev

    return list(unique_events.values())
