import asyncio
import re
from typing import List, Optional
from urllib.parse import urljoin
import aiohttp
from bs4 import BeautifulSoup

# Исключаемый номер шапки (8-8634-61-00-13)
EXCLUDED_PHONE_DIGITS = "8634610013"

# Список подразделений для проверки локации
MUSEUM_BRANCHES = [
    {
        "name": "Дворец Алфераки",
        "keys": ["алфераки", "историко-краеведческий"],
        "address": "ул. Фрунзе, 41",
        "tag": "#ДворецАлфераки",
    },
    {
        "name": "Домик Чехова",
        "keys": ["домик чехова"],
        "address": "ул. Чехова, 69",
        "tag": "#ДомикЧехова",
    },
    {
        "name": "Лавка Чеховых",
        "keys": ["лавка чеховых"],
        "address": "ул. Александровская, 100",
        "tag": "#ЛавкаЧеховых",
    },
    {
        "name": "Музей И.Д. Василенко",
        "keys": ["василенко"],
        "address": "ул. Чехова, 88",
        "tag": "#МузейВасиленко",
    },
    {
        "name": "Музей градостроительства и быта",
        "keys": ["градостроительства"],
        "address": "ул. Фрунзе, 80",
        "tag": "#МузейГрадостроительства",
    },
]


def extract_image_url(
    soup: BeautifulSoup, base_url: str = "https://tgliamz.ru"
) -> Optional[str]:
    """Извлекает обложку новости из блока .news-item-img."""
    img_tag = soup.select_one(".news-item-img img")
    if img_tag and img_tag.get("src"):
        return urljoin(base_url, img_tag["src"])

    link_tag = soup.select_one(".news-item-img a[href]")
    if link_tag and link_tag.get("href"):
        return urljoin(base_url, link_tag["href"])

    return None


def extract_targeted_phones(text_block: str) -> List[tuple]:
    """Извлекает целевые телефоны из текста, отсекая общий номер музея."""
    phone_pattern = r"(?:\+?7|8)?[\s\(\-]*\d{3,4}[\s\)\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}|\b\d{2}[\s\-]?\d{2}[\s\-]?\d{2}\b"

    # Ищем контекст со справками/бронированием
    match = re.search(
        r"(?:телефон|тел\.?|справки|бронирование)[^:\n]*[:\s]+([^\n<]+)",
        text_block,
        re.IGNORECASE,
    )
    target_chunk = match.group(1) if match else text_block

    raw_phones = re.findall(phone_pattern, target_chunk)
    result = []
    seen = set()

    for raw in raw_phones:
        digits = re.sub(r"\D", "", raw)

        # Пропускаем номер музея или слишком короткий мусор
        if EXCLUDED_PHONE_DIGITS in digits or len(digits) < 6:
            continue

        # Форматирование номеров
        if len(digits) == 6:  # Городской Таганрога без кода
            display = f"8 (8634) {digits[:2]}-{digits[2:4]}-{digits[4:]}"
            tel = f"+78634{digits}"
        elif len(digits) in (10, 11):
            if digits.startswith("8"):
                digits = "7" + digits[1:]
            elif len(digits) == 10:
                digits = "7" + digits

            if len(digits) == 11 and digits[1] == "9":  # Мобильный
                display = (
                    f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
                )
            else:  # Городской с кодом
                display = f"8 ({digits[1:5]}) {digits[5:7]}-{digits[7:9]}-{digits[9:]}"
            tel = f"+{digits}"
        else:
            continue

        if tel not in seen:
            seen.add(tel)
            result.append((display, tel))

    return result


async def parse_detail_test(
    session: aiohttp.ClientSession, url: str
) -> dict:
    """Тестовый парсинг отдельного анонса."""
    data = {
        "url": url,
        "title": "",
        "image_url": None,
        "date_str": "",
        "time_str": "",
        "location": "ТГЛИАМЗ",
        "phones": [],
        "buy_ticket_url": "",
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                print(f"Ошибка HTTP {resp.status} для {url}")
                return data

            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")

            # Заголовок новости
            title_tag = soup.select_one(
                ".news-detail-title, h1, .news-item-title"
            )
            if title_tag:
                data["title"] = title_tag.get_text(strip=True)

            # 1. Картинка
            data["image_url"] = extract_image_url(soup)

            # 2. Текст новости
            content_div = soup.select_one(".news-item-text") or soup
            text_block = content_div.get_text()

            # 3. Телефоны
            data["phones"] = extract_targeted_phones(text_block)

            # 4. Билеты
            buy_link = soup.select_one("a[href*='vmuzey.com/event/']")
            if buy_link:
                data["buy_ticket_url"] = buy_link["href"].strip()

            # 5. Дата и Время
            date_match = re.search(
                r"(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))",
                text_block,
                re.I,
            )
            if date_match:
                data["date_str"] = date_match.group(1)

            time_match = re.search(
                r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", text_block, re.I
            )
            if time_match:
                data["time_str"] = time_match.group(1).replace(".", ":")

            # 6. Локация
            for branch in MUSEUM_BRANCHES:
                if any(k in text_block.lower() for k in branch["keys"]):
                    data["location"] = branch["name"]
                    break

    except Exception as e:
        print(f"Ошибка запроса {url}: {e}")

    return data


async def main():
    # Список реальных ссылок анонсов с сайта для теста
    test_urls = [
        "https://tgliamz.ru/calendar/detail.php?ID=14407",  # Замени при необходимости на актуальные ID
        "https://tgliamz.ru/calendar/",
    ]

    async with aiohttp.ClientSession() as session:
        # Если проверяем страницу всего календаря, соберем с нее ссылки на детали
        async with session.get(
            "https://tgliamz.ru/calendar/", timeout=10
        ) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                links = soup.select(
                    ".news-item a[href*='detail.php'], .news-list a[href*='detail.php']"
                )
                test_urls = list(
                    set(
                        [
                            urljoin("https://tgliamz.ru", l["href"])
                            for l in links[:3]
                        ]
                    )
                )

        print(f"--- НАЧИНАЕМ ТЕСТ НА {len(test_urls)} СТРАНИЦАХ ---\n")

        for idx, url in enumerate(test_urls, 1):
            res = await parse_detail_test(session, url)
            print(f"[{idx}] Ссылка: {res['url']}")
            print(f"    Заголовок: {res['title']}")
            print(f"    Картинка:   {res['image_url']}")
            print(f"    Дата/Время: {res['date_str']} | {res['time_str']}")
            print(f"    Площадка:   {res['location']}")
            print(f"    Телефоны:   {res['phones']}")
            print(f"    Купить:     {res['buy_ticket_url']}")
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
