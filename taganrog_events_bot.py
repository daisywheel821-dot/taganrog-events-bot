# ===================== НАСТРОЙКИ СТИЛЯ И ИКОНОК =====================
# Вы можете изменить любая иконку, заменять их на символы или сделать пустыми ""
ICONS = {
    # Категории
    "cat_theatre_month": "🎭 ",
    "cat_theatre_today": "🎭 ",
    "cat_cinema": "🎬 ",
    "cat_museum": "🎨 ",
    "cat_events": "🎪 ",
    "cat_greenwich": "🌊 ",
    "cat_aqualazur": "🎢 ",
    "cat_golden_horse": "🐴 ",

    # Поля событий
    "date": "📅 ",
    "time": "🕐 ",
    "location": "📍 ",
    "description": "📝 ",
    "price": "💰 ",
    "ticket": "🎟 ",
    "phone": "📞 ",
}

# Если вы хотите МИНИМАЛИЗМ БЕЗ ИКОНОК, просто раскомментируйте нижний блок:
# ICONS = {k: "" for k in ICONS}


# ===================== ФОРМАТТЕР =====================
def format_caption(event: Event) -> str:
    title = html.escape(event.title)
    date_str = html.escape(event.date_str)
    time_str = html.escape(event.time_str)
    location = html.escape(event.location)
    address = html.escape(event.address)
    description = html.escape(event.description)
    prices = html.escape(event.prices)
    phone = html.escape(event.phone)
    tickets_url = html.escape(event.tickets_url)

    lines = []

    # 1. Шапка категории (с подтягиванием иконки из словаря)
    category_titles = {
        Category.THEATRE_MONTH: f"{ICONS['cat_theatre_month']}<b>АФИША ТЕАТРА НА МЕСЯЦ</b>",
        Category.THEATRE_TODAY: f"{ICONS['cat_theatre_today']}<b>ТЕАТР СЕГОДНЯ</b>",
        Category.CINEMA: f"{ICONS['cat_cinema']}<b>КИНО В ТАГАНРОГЕ</b>",
        Category.MUSEUM: f"{ICONS['cat_museum']}<b>МУЗЕИ И ВЫСТАВКИ</b>",
        Category.EVENTS: f"{ICONS['cat_events']}<b>СОБЫТИЯ И КОНЦЕРТЫ</b>",
        Category.GREENWICH: f"{ICONS['cat_greenwich']}<b>ГРИНВИЧ ПАРК SPA</b>",
        Category.AQUALAZUR: f"{ICONS['cat_aqualazur']}<b>АКВАПАРК «ЛАЗУРНЫЙ»</b>",
        Category.GOLDEN_HORSE: f"{ICONS['cat_golden_horse']}<b>КЛУБ «ГОЛДЕН ХОРС»</b>",
    }
    
    header = category_titles.get(event.category, "<b>АФИША ТАГАНРОГА</b>")
    lines.append(header)

    # 2. Название события
    lines.append(f"\n<b>{title}</b>\n")

    # 3. Дата и Время
    date_time_parts = []
    if date_str:
        date_time_parts.append(f"{ICONS['date']}{date_str}")
    if time_str:
        date_time_parts.append(f"{ICONS['time']}{time_str}")
    if date_time_parts:
        lines.append(" | ".join(date_time_parts))

    # 4. Локация и Адрес
    loc_icon = ICONS['location']
    if location and address:
        lines.append(f"{loc_icon}{location} ({address})")
    elif location:
        lines.append(f"{loc_icon}{location}")
    elif address:
        lines.append(f"{loc_icon}{address}")

    # 5. Описание
    if description:
        lines.append(f"\n{ICONS['description']}{description}")

    # 6. Цена
    if prices:
        lines.append(f"{ICONS['price']}{prices}")

    # 7. Контакты и ссылки
    contact_parts = []
    if tickets_url:
        contact_parts.append(f"{ICONS['ticket']}<a href='{tickets_url}'>Купить билет / Подробнее</a>")
    if phone:
        contact_parts.append(f"{ICONS['phone']}{phone}")
    
    if contact_parts:
        lines.append("\n" + "\n".join(contact_parts))

    # 8. Хэштеги
    hashtags = {
        Category.THEATRE_MONTH: "#Таганрог #Театр #Афиша",
        Category.THEATRE_TODAY: "#Таганрог #Театр #Спектакль",
        Category.CINEMA: "#Таганрог #Кино #Афиша",
        Category.MUSEUM: "#Таганрог #Музей #Выставка",
        Category.EVENTS: "#Таганрог #Афиша #Концерт",
        Category.GREENWICH: "#Таганрог #Отдых #SPA",
        Category.AQUALAZUR: "#Таганрог #Отдых #Аквапарк",
        Category.GOLDEN_HORSE: "#Таганрог #Развлечения",
    }
    
    lines.append(f"\n{hashtags.get(event.category, '#Таганрог #Афиша')}")

    return "\n".join(lines)
