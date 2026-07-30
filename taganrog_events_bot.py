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
        "is_shop": False
    }
    try:
        async with session.get(detail_url, timeout=12) as resp:
            if resp.status == 200:
                html_text = await resp.text()
                
                # Игнорируем только товары сувенирной лавки
                if is_souvenir_shop_item(html_text):
                    data["is_shop"] = True
                    return data

                soup = BeautifulSoup(html_text, "html.parser")

                # 1. Поиск ссылки на ВМузей / Пушкинскую карту (если она есть)
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    link_text = a_tag.get_text(strip=True).lower()
                    
                    if "vmuzey.com" in href:
                        data["buy_ticket_url"] = href
                        break
                    elif "купить билет" in link_text and href.startswith("http"):
                        if not data["buy_ticket_url"]:
                            data["buy_ticket_url"] = href

                # 2. Извлечение текста и описания
                content_block = soup.select_one(
                    ".detail-text, .news-detail, .content-text, .detail_text, "
                    ".workarea, .content, article, .event-detail, .page-content"
                )
                page_full_text = soup.get_text()
                data["phones"] = extract_all_phones(page_full_text)

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

                # 3. Определение филиала музея
                text_to_check = page_full_text.lower()
                for branch in MUSEUM_BRANCHES:
                    if any(k in text_to_check for k in branch["keys"]):
                        data["location"] = branch["name"]
                        data["address"] = branch["address"]
                        data["branch_tag"] = branch["tag"]
                        break

                # 4. Поиск даты, времени и цены
                date_match = re.search(
                    r"((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)?,?\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))", 
                    page_full_text, re.I
                )
                if date_match:
                    data["date_str"] = date_match.group(1).capitalize()

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
    
    # Сканируем не только /calendar/, но и общую афишу/новости
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

                    # Собираем ВСЕ ссылки без ограничений по классам
                    for a in soup.find_all("a", href=True):
                        href = a["href"].strip()
                        # Сохраняем любые ссылки на внутренние публикации
                        if any(part in href for part in ["/calendar/", "/news/", "/afisha/", "/events/"]):
                            if href not in ["/calendar/", "/news/", "/afisha/"]:
                                full_url = urljoin(base_url, href)
                                candidate_urls.add(full_url)
        except Exception as e:
            logger.error(f"Ошибка при сборе ссылок с {start_url}: {e}")

    logger.info(f"Всего найдено кандидатов-ссылок на события ТГЛИАМЗ: {len(candidate_urls)}")

    for event_url in candidate_urls:
        detail_data = await parse_tgliamz_detail(session, event_url)
        
        # Пропускаем только если это страница магазина/сувениров
        if detail_data["is_shop"]:
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

                    # Находим изображение
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
                            buy_ticket_url=detail_data["buy_ticket_url"], # Может быть пустым — и это нормально!
                            image_url=image_url,
                            tags=final_tags
                        )
                    )
        except Exception as e:
            logger.warning(f"Ошибка обработки страницы события {event_url}: {e}")

    # Удаляем возможные дубликаты
    unique_events = {}
    for ev in events:
        if ev.event_id not in unique_events:
            unique_events[ev.event_id] = ev

    return list(unique_events.values())
