# ============================================================
# ПАТЧ: блок «Афиша театра» (Пятница)
# Источник: tag.afishagoroda.ru/events/teatr
# Движок сайта тот же, что и у концертов, поэтому переиспользуем
# parse_afishagoroda_detail() один в один — ничего нового парсить не нужно.
# Ниже — 4 изменения в taganrog_events_bot.py, в порядке применения.
# ============================================================


# ------------------------------------------------------------
# 1) HEADERS_BY_CATEGORY — добавить строку "theater"
#    (место: рядом с остальными категориями)
# ------------------------------------------------------------
HEADERS_BY_CATEGORY = {
    "museum": "МУЗЕЙНАЯ АФИША ТАГАНРОГА",
    "concerts": "АФИША КОНЦЕРТОВ ТАГАНРОГА",
    "cinema": "АФИША КИНО ТАГАНРОГА",
    "spa": "СПА И ТЕРМАЛЬНЫЕ КОМПЛЕКСЫ ТАГАНРОГА",
    "theater": "АФИША ТЕАТРА ТАГАНРОГА",   # <-- новое
}


# ------------------------------------------------------------
# 2) Новая функция парсера — вставить сразу после
#    parse_afishagoroda_concerts() (в разделе
#    "ПАРСИНГ AFISHAGORODA (КОНЦЕРТЫ)").
#
#    Полностью повторяет логику parse_afishagoroda_concerts():
#    те же find_all по /events/<slug>, тот же AFISHAGORODA_EXCLUDED_SLUGS
#    (там уже есть "teatr" — он и так исключался как ссылка на раздел),
#    тот же lookahead в 90 дней, та же детальная функция
#    parse_afishagoroda_detail() — она не завязана на категорию контента,
#    просто читает "Когда:/Где:/Стоимость билетов:/Возрастные ограничения:".
#
#    Единственные отличия от концертов:
#    - url раздела: /events/teatr вместо /events/koncert
#    - category="theater", event_type="Спектакль"
#    - фильтр "не тот раздел" смотрит на слова про концерты/экскурсии/etc,
#      а не про спектакли/театр (иначе спектакль сам себя вырежет)
#    - хештеги #Театр вместо #Концерт
# ------------------------------------------------------------
async def parse_afishagoroda_theater(session: aiohttp.ClientSession) -> List[Event]:
    events = []
    seen_urls = set()
    url = f"{AFISHAGORODA_BASE}/events/teatr"

    # Тот же лимит, что и у концертов — не спамить слишком дальними анонсами.
    lookahead_limit = date.today() + timedelta(days=90)

    try:
        async with session.get(url, headers=HEADERS, timeout=12) as resp:
            if resp.status != 200:
                logger.error(f"afishagoroda theater: неожиданный статус {resp.status}")
                return events
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")

            candidate_links = soup.find_all("a", href=re.compile(r'/events/[a-z0-9\-]+/?$', re.I))
            logger.info(f"afishagoroda theater: найдено ссылок-кандидатов: {len(candidate_links)}")

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

                    title = detail_data.get("title") or fallback_title
                    if not title:
                        continue

                    # Заслон от чужих категорий, просочившихся через
                    # "рекомендуем также"/"похожие мероприятия" на странице театра.
                    non_theater_hints = ("концерт", "экскурси", "мастер-класс", "выставк", "лекци")
                    if any(hint in title.lower() for hint in non_theater_hints):
                        logger.info(f"Пропуск (похоже, не спектакль по названию): {title}")
                        continue

                    parsed_date = detail_data.get("parsed_date")
                    if not parsed_date:
                        # У событий с несколькими показами/диапазоном дат формат
                        # страницы другой — по общему правилу проекта такие
                        # события пропускаются, а не показываются без даты.
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
                        category="theater",
                        event_type="Спектакль",
                        date_str=detail_data.get("date_str", ""),
                        parsed_date=parsed_date,
                        time_str=detail_data.get("time_str", ""),
                        location=detail_data.get("location", ""),
                        prices=detail_data.get("prices", ""),
                        age_rating=detail_data.get("age_rating", ""),
                        hashtags=["#Театр", "#Таганрог", "#афиша"],
                        buy_ticket_url=detail_data.get("buy_ticket_url", ""),
                        image_url=detail_data.get("image_url"),
                    )
                    events.append(event)
                    logger.info(f"Событие добавлено к отправке: {title} ({parsed_date})")
                except Exception as item_err:
                    logger.error(f"Ошибка при обработке карточки afishagoroda '{fallback_title or href}': {item_err}")
                    continue
    except Exception as e:
        logger.error(f"Ошибка при парсинге afishagoroda (театр): {e}")
    return events


# ------------------------------------------------------------
# 3) WEEKDAY_BLOCKS — добавить пятницу (weekday() == 4)
# ------------------------------------------------------------
WEEKDAY_BLOCKS = {
    0: "museum",
    1: "concerts",
    2: "spa",
    3: "cinema",
    4: "theater",   # <-- новое
}


# ------------------------------------------------------------
# 4) run_block() — добавить ветку диспетчера
# ------------------------------------------------------------
async def run_block(block_name: str, session: aiohttp.ClientSession) -> List[Event]:
    """Запускает нужный парсер по названию блока."""
    if block_name == "museum":
        return await parse_tgliamz_museums(session)
    elif block_name == "concerts":
        return await parse_afishagoroda_concerts(session)
    elif block_name == "cinema":
        return await parse_charly_cinema(session)
    elif block_name == "spa":
        return await parse_spa_block(session)
    elif block_name == "theater":                              # <-- новое
        return await parse_afishagoroda_theater(session)        # <-- новое
    else:
        logger.error(f"Неизвестный блок: {block_name}")
        return []


# ------------------------------------------------------------
# Тест вручную:
#   в workflow-файле поставить  FORCE_BLOCK: theater
# ------------------------------------------------------------
