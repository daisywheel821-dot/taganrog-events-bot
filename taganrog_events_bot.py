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

    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if time_str:
        lines.append(f"<b>Время:</b> {time_str}")

    if location:
        lines.append(f"<b>Место:</b> {location}")
    if address:
        lines.append(f"<b>Адрес:</b> {address}")

    if prices:
        lines.append(f"<b>Стоимость:</b> {prices}")

    if description:
        lines.append(f"\n{description}")

    # Блок ссылок и прямых телефонов (без общего номера музея)
    if tickets_url:
        lines.append(f"\n<a href='{tickets_url}'>Официальная страница / Билеты</a>")

    # Исключаем общий номер +7 (8634) 38-34-96 из вывода
    filtered_phones = [
        (disp, tel) for disp, tel in event.phones 
        if "38-34-96" not in disp and "383496" not in tel
    ]

    if filtered_phones:
        lines.append("\n<b>Запись и вопросы по телефону:</b>")
        for disp, tel in filtered_phones:
            lines.append(f"📞 <a href='tel:{tel}'>{disp}</a>")

    if event.tags:
        lines.append("\n" + " ".join(event.tags))
    else:
        lines.append("\n#Таганрог #Афиша")

    return "\n".join(lines)


# ===================== ПАРСИНГ ДЕТАЛЕЙ =====================
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

                content_block = soup.select_one(".detail-text, .news-detail, .content-text, .detail_text, .workarea, .content")
                full_text = ""
                if content_block:
                    for s in content_block(["script", "style"]):
                        s.extract()

                    paragraphs = []
                    important_notes = []

                    for el in content_block.find_all(["p", "div"]):
                        txt = el.get_text(strip=True)
                        if len(txt) < 15 or txt.startswith("Купить"):
                            continue

                        # Автоматический отлов условий записи и возрастных ограничений
                        if any(phrase in txt.lower() for phrase in ["предварительная запись", "количество мест ограничено", "приглашаются участники", "опыт не важен"]):
                            if txt not in important_notes:
                                important_notes.append(f"<b>{txt}</b>")
                        else:
                            if txt not in paragraphs and not txt.startswith("Тел"):
                                paragraphs.append(txt)

                    # Формируем описание: сперва основные абзацы, затем акцентный блок «Важно»
                    desc_parts = []
                    if paragraphs:
                        desc_parts.append("\n\n".join(paragraphs[:2]))
                    if important_notes:
                        desc_parts.append("📌 <b>Важно:</b>\n" + "\n".join(important_notes))

                    data["description"] = "\n\n".join(desc_parts)
                    full_text = " ".join(paragraphs + important_notes)

                if not full_text:
                    full_text = soup.get_text()

                text_to_check = full_text.lower()
                for branch in MUSEUM_BRANCHES:
                    if any(k in text_to_check for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                date_match = re.search(r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", full_text, re.I)
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()

                time_match = re.search(r"\bв\s*(\d{1,2}[\.\:]\d{2})\b", full_text, re.I)
                if time_match:
                    data["time_str"] = time_match.group(1).replace(".", ":")

                price_match = re.search(r"(?:стоимость[^\d]*?|билет[а-я]*\s*–?\s*)(\d+\s*руб[а-я]*)", full_text, re.I)
                if price_match:
                    data["prices"] = price_match.group(1)

                data["phones"] = extract_all_phones(full_text)

    except Exception as e:
        logger.warning(f"Ошибка получения деталей страницы {detail_url}: {e}")
    return data
