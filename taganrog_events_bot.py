import os
import requests
from bs4 import BeautifulSoup
from telegram import Bot
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import logging
import random
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def parse_afisha_goroda():
    """Парсит афишу с afishagoroda.ru (Афиша Города)"""
    try:
        url = "https://tag.afishagoroda.ru/events"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        
        soup = BeautifulSoup(response.content, 'html.parser')
        events = []
        
        # Ищем информацию о мероприятиях
        event_containers = soup.find_all(['div', 'article'], class_=['event', 'meropriyatie', 'item', 'card'])
        
        # Если контейнеры не найдены, ищем по названиям
        if not event_containers:
            links = soup.find_all('a', href=True)
            for link in links[:15]:
                text = link.get_text(strip=True)
                if len(text) > 10 and len(text) < 150:
                    href = link.get('href', '')
                    if any(keyword in text.lower() for keyword in ['концерт', 'спектакль', 'выставка', 'фестиваль', 'кино', 'театр']):
                        events.append({
                            'title': text,
                            'type': 'Театр/Концерт/Кино',
                            'location': 'Таганрог',
                            'date': 'Ближайшие дни',
                            'price': 'Зависит от события',
                            'emoji': '🎭',
                            'source': 'Афиша Города',
                            'url': f"https://tag.afishagoroda.ru{href}" if href.startswith('/') else href,
                            'link_text': 'Купить билеты →'
                        })
        
        logger.info(f"Found {len(events)} events on Афиша Города")
        return events[:5]
        
    except Exception as e:
        logger.error(f"Error parsing Афиша Города: {e}")
        return []

def parse_kassy():
    """Парсит билеты и события с taganrog.kassy.ru"""
    try:
        url = "https://taganrog.kassy.ru/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        
        soup = BeautifulSoup(response.content, 'html.parser')
        events = []
        
        # Ищем события в списке
        event_elements = soup.find_all(['div', 'li'], class_=['event-item', 'ticket-item', 'item'])
        
        if not event_elements:
            # Ищем по заголовкам
            titles = soup.find_all(['h2', 'h3', 'span'], class_=['title', 'name', 'event-name'])
            for title in titles[:10]:
                text = title.get_text(strip=True)
                if len(text) > 8:
                    # Ищем цену рядом
                    price_elem = title.find_parent().find(['span', 'div'], class_=['price', 'cost'])
                    price = price_elem.get_text(strip=True) if price_elem else 'Смотри на сайте'
                    
                    events.append({
                        'title': text,
                        'type': 'Концерт/Спектакль',
                        'date': 'Уточняется',
                        'price': price,
                        'emoji': '🎪',
                        'source': 'Kassy',
                        'url': 'https://taganrog.kassy.ru/',
                        'link_text': 'Купить билеты →'
                    })
        
        logger.info(f"Found {len(events)} events on Kassy")
        return events[:5]
        
    except Exception as e:
        logger.error(f"Error parsing Kassy: {e}")
        return []

def parse_culture_ru():
    """Парсит афишу с culture.ru (Google Культура)"""
    try:
        url = "https://www.culture.ru/afisha/rostovskaya-oblast-taganrog"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        
        soup = BeautifulSoup(response.content, 'html.parser')
        events = []
        
        # Ищем события по классам Culture.ru
        event_blocks = soup.find_all(['div', 'article'], class_=['EventCard', 'afisha-item', 'event'])
        
        if event_blocks:
            for block in event_blocks[:5]:
                title_elem = block.find(['h2', 'h3', 'span'])
                if title_elem:
                    text = title_elem.get_text(strip=True)
                    if len(text) > 5:
                        events.append({
                            'title': text,
                            'type': 'Выставка/Мероприятие',
                            'date': 'Уточняется',
                            'location': 'Таганрог',
                            'price': 'Смотри на сайте',
                            'emoji': '🎨',
                            'source': 'Culture.ru',
                            'url': 'https://www.culture.ru/afisha/rostovskaya-oblast-taganrog',
                            'link_text': 'Подробнее →'
                        })
        
        logger.info(f"Found {len(events)} events on Culture.ru")
        return events[:5]
        
    except Exception as e:
        logger.error(f"Error parsing Culture.ru: {e}")
        return []

def get_premium_venues():
    """Премиум-места для взрослой аудитории 18-50 лет"""
    return [
        {
            'title': 'Гринвич Парк - Термальный комплекс',
            'type': '🌊 Spa & Развлечения',
            'date': 'Ежедневно 10:00-23:00',
            'location': 'ул. Адмирала Крюйса, 2а',
            'price': 'от 500 ₽ за посещение',
            'emoji': '🏊',
            'description': '5 бассейнов, русская баня, хаммам, сауна, гидромассаж, SPA-процедуры, ресторан. Вид на Азовское море!',
            'source': 'Гринвич Парк',
            'url': 'https://broni.travel/rostovskaya-oblast/taganrog/gostinitsa-greenwich-park-grinvich-park-taganrog/',
            'link_text': 'Узнать о скидках →',
            'discount': 'Скидки до 45% по купонам'
        },
        {
            'title': 'Голден Хорс - Развлекательный комплекс',
            'type': '🎰 Игры и развлечения',
            'date': 'Ежедневно 12:00-02:00',
            'location': 'Центр города',
            'price': 'Вход: бесплатно',
            'emoji': '🎯',
            'description': 'Игровые автоматы, бильярд, кегельбан, рестобар, живая музыка по выходам. Отличное место для вечеринок!',
            'source': 'Голден Хорс',
            'url': 'https://taganrog.biglion.ru/',
            'link_text': 'Посмотреть программу →',
            'discount': 'Специальные предложения по выходам'
        },
        {
            'title': 'Таганрогский театр драмы имени Чехова',
            'type': '🎭 Театр',
            'date': 'Спектакли Вт-Вс 19:00',
            'location': 'ул. Петровская, 100',
            'price': '300-600 ₽',
            'emoji': '🎭',
            'description': 'Классические пьесы Чехова, современные постановки, гастроли известных театров.',
            'source': 'Театр Чехова',
            'url': 'https://tag.afishagoroda.ru/events/teatr',
            'link_text': 'Расписание и билеты →',
            'discount': 'Скидки по Пушкинской карте'
        },
        {
            'title': 'Таганрогская филармония',
            'type': '🎵 Концерты',
            'date': 'По расписанию',
            'location': 'ул. Петровская, 93',
            'price': '300-800 ₽',
            'emoji': '🎵',
            'description': 'Концерты классической музыки, оперные вечера, звёзды российской эстрады.',
            'source': 'Филармония',
            'url': 'https://tag.afishagoroda.ru/events',
            'link_text': 'Программа →',
            'discount': 'Абонементы со скидками'
        },
        {
            'title': 'Кинотеатр "Современник"',
            'type': '🎬 Кино',
            'date': 'Ежедневно 17:00-23:00',
            'location': 'Центр города',
            'price': '250-350 ₽',
            'emoji': '🎬',
            'description': 'Премьеры фильмов, блокбастеры, артхаусное кино, современные залы с комфортом.',
            'source': 'Кинотеатр',
            'url': 'https://tag.afishagoroda.ru/events',
            'link_text': 'Сегодняшний репертуар →',
            'discount': 'Скидки по картам и акции'
        },
        {
            'title': 'Таганрогский художественный музей',
            'type': '🎨 Выставки',
            'date': 'Вт-Вс 10:00-18:00',
            'location': 'ул. Александровская, 70',
            'price': '350 ₽',
            'emoji': '🎨',
            'description': 'Коллекция русского искусства, временные выставки, картины Поленова, Левитана, современные авторы.',
            'source': 'Музей',
            'url': 'https://www.culture.ru/afisha/rostovskaya-oblast-taganrog',
            'link_text': 'Текущие выставки →',
            'discount': 'Льготы для студентов и пенсионеров'
        },
        {
            'title': 'Дворец культуры "Фестивальный"',
            'type': '🎪 Шоу и фестивали',
            'date': 'По расписанию',
            'location': 'Центр города',
            'price': '200-500 ₽',
            'emoji': '🎪',
            'description': 'Концерты, фестивали, праздничные шоу, тематические вечера, выставки.',
            'source': 'ДК Фестивальный',
            'url': 'https://tag.afishagoroda.ru/events',
            'link_text': 'Афиша ДК →',
            'discount': 'Сезонные скидки'
        },
    ]

def format_event_for_max(event):
    """Форматирует событие для поста в MAX"""
    emoji = event.get('emoji', '📍')
    title = event.get('title', 'Событие')
    event_type = event.get('type', '')
    date = event.get('date', 'ближайшие дни')
    location = event.get('location', 'Таганрог')
    price = event.get('price', 'Информация уточняется')
    description = event.get('description', '')
    discount = event.get('discount', '')
    link_text = event.get('link_text', 'Подробнее →')
    url = event.get('url', '')
    
    # Лимитируем длину названия
    if len(title) > 70:
        title = title[:67] + '...'
    
    formatted = f"""{emoji} {title.upper()}

{event_type}

📅 {date}
📍 {location}

{description}

💰 {price}"""
    
    if discount:
        formatted += f"\n🎁 {discount}"
    
    formatted += f"\n\n🔗 {link_text}\n\n#Таганрог #развлечения #событие"
    
    return formatted.strip()

async def send_daily_events():
    """Отправляет события в Telegram в 9:00"""
    try:
        logger.info("Fetching events from Google sources...")
        
        all_events = []
        
        # Парсим из разных источников
        logger.info("Parsing Афиша Города...")
        afisha_events = parse_afisha_goroda()
        all_events.extend(afisha_events)
        
        logger.info("Parsing Kassy...")
        kassy_events = parse_kassy()
        all_events.extend(kassy_events)
        
        logger.info("Parsing Culture.ru...")
        culture_events = parse_culture_ru()
        all_events.extend(culture_events)
        
        # Добавляем премиум-места
        logger.info("Adding premium venues...")
        premium_places = get_premium_venues()
        all_events.extend(premium_places)
        
        if not all_events:
            all_events = get_premium_venues()
        
        # Убираем дубликаты
        unique_events = {}
        for event in all_events:
            title_key = event['title'][:30]
            if title_key not in unique_events:
                unique_events[title_key] = event
        
        all_events = list(unique_events.values())
        
        # Выбираем 4 события (2 премиум + 2 актуальных)
        premium = [e for e in all_events if e.get('source') in ['Гринвич Парк', 'Голден Хорс', 'Театр Чехова']]
        other = [e for e in all_events if e.get('source') not in ['Гринвич Парк', 'Голден Хорс', 'Театр Чехова']]
        
        selected_premium = random.sample(premium, min(2, len(premium)))
        selected_other = random.sample(other, min(2, len(other)))
        selected_events = selected_premium + selected_other
        
        if len(selected_events) < 4:
            selected_events = random.sample(all_events, min(4, len(all_events)))
        
        bot = Bot(token=TELEGRAM_TOKEN)
        
        message = "📅 СОБЫТИЯ И РАЗВЛЕЧЕНИЯ ТАГАНРОГА (18-50 лет)\n\n"
        message += f"📆 {datetime.now().strftime('%d.%m.%Y')} | Каждый день новые мероприятия!\n"
        message += "=" * 50 + "\n\n"
        
        # Основные события
        for i, event in enumerate(selected_events, 1):
            formatted = format_event_for_max(event)
            message += f"【 {i}. {event.get('type', 'СОБЫТИЕ')} 】\n{formatted}\n\n"
            message += "─" * 50 + "\n\n"
        
        # Блок с предстоящими мероприятиями и скидками
        message += "🎯 ПРЕДСТОЯЩИЕ МЕРОПРИЯТИЯ И СКИДКИ\n"
        message += "═" * 50 + "\n\n"
        message += "🔥 Горячие предложения:\n"
        message += "• Гринвич Парк: скидки до 45% на SPA-процедуры\n"
        message += "• Театр Чехова: скидки по Пушкинской карте\n"
        message += "• Все кинотеатры: скидки по картам постоянного покупателя\n\n"
        
        message += "🎭 Где купить билеты:\n"
        message += "📱 Афиша Города: tag.afishagoroda.ru\n"
        message += "🎟️ Kassy.ru: taganrog.kassy.ru\n"
        message += "📺 Culture.ru: www.culture.ru\n\n"
        
        message += "💡 Следи за обновлениями ежедневно в 9:00!\n"
        message += "#Таганрог #развлечения #афиша #скидки"
        
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        
        logger.info("Events sent successfully")
        
    except Exception as e:
        logger.error(f"Error sending events: {e}")
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Проблема при загрузке афиши. Попробуем завтра в 9:00!\n\nОшибка: {str(e)[:80]}"
            )
        except:
            pass

async def main():
    """Запускает планировщик"""
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    scheduler.add_job(
        send_daily_events,
        'cron',
        hour=9,
        minute=0,
        timezone="Europe/Moscow"
    )
    
    scheduler.start()
    
    logger.info("Bot started. Scheduled for 9:00 Moscow time daily")
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
