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
            lines.append("<i>Таганрогский музей-заповедник</i>\n")

    # 2. Название события
    lines.append(f"<b>{title}</b>\n")

    # 3. Детали (Дата, Время, Цена, Запись/Бронирование)
    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")
    if prices:
        lines.append(f"<b>Стоимость билета:</b> {prices}")

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


# ===================== ОБНОВЛЕННЫЙ ДЕТАЛЬНЫЙ ПАРСИНГ ТГЛИАМЗ =====================
async def parse_tgliamz_detail(session: aiohttp.ClientSession, detail_url: str) -> dict:
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

                # Категория / Тип программы
                type_el = soup.select_one(".category-title, .subtitle, .event-type, .news-category, .section-title")
                if type_el:
                    data["event_type"] = type_el.get_text(strip=True)

                # Поиск главной картинки афиши на странице детализации
                img_el = soup.select_one(".detail-image img, .news-detail img, .content img, .workarea img")
                if img_el and img_el.get("src"):
                    data["image_url"] = urljoin("https://tgliamz.ru", img_el["src"])

                # Поиск ссылки на онлайн-билеты vmuzey.com
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    if "vmuzey.com" in href:
                        data["buy_ticket_url"] = href
                        break

                content_block = soup.select_one(".detail-text, .news-detail, .content-text, .detail_text, .workarea, .content")
                
                if content_block:
                    content_text = content_block.get_text()
                    
                    # Забор телефонов из текста анонса
                    data["phones"] = extract_all_phones(content_text)

                    # Проверка на необходимость предварительной записи / бронирования
                    if any(phrase in content_text.lower() for phrase in [
                        "предварительная запись обязательна", 
                        "запись по телефону", 
                        "бронирование мест обязательно",
                        "предварительная запись"
                    ]):
                        data["requires_booking"] = True

                # Определение филиала и адреса
                page_full_text = soup.get_text()
                text_to_check = page_full_text.lower()
                for branch in MUSEUM_BRANCHES:
                    if any(k in text_to_check for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # Парсинг даты и времени
                date_match = re.search(r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", page_full_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()

                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", page_full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*)(\d+\s*руб[а-я]*[^\.\n]*)", page_full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1)

    except Exception as e:
        logger.warning(f"Ошибка парсинга {detail_url}: {e}")
    return data
