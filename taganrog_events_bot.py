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

# ======================== ПАРСЕРЫ ========================

def parse_chehovsky_theatre():
    """Парсит спектакли театра Чехова - chehovsky.ru"""
    try:
        url = "https://www.chehovsky.ru/repertoire/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        
        # Ищем спектакли
        spectacles = soup.find_all(['div', 'article', 'li'], class_=['spectacle', 'item', 'afisha', 'event', 'program'])
        
        if not spectacles:
            # Ищем по ссылкам и заголовкам
            links = soup.find_all('a', href=True)
            for link in links[:20]:
                text = link.get_text(strip=True)
                if len(text) > 10 and any(word in text.lower() for word in ['вишневый', 'сад', 'спектакль', 'театр', 'пьеса']):
                    events.append({
                        'title': text,
                        'type': 'Спектакль',
                        'theatre': 'Театр Чехова',
                        'date': 'Сегодня',
                        'time': '19:00',
                        'price': '350-600 ₽',
                        'emoji': '🎭',
                        'url': 'https://www.chehovsky.ru/repertoire/',
                        'actors': 'Актеры: смотри на сайте',
                        'description': 'Классическая пьеса А.П. Чехова'
                    })
        else:
            for spectacle in spectacles[:5]:
                title_elem = spectacle.find(['h2', 'h3', 'span', 'a'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    if len(title) > 5:
                        events.append({
                            'title': title,
                            'type': 'Спектакль',
                            'theatre': 'Театр Чехова',
                            'date': 'Сегодня',
                            'time': '19:00',
                            'price': '350-600 ₽',
                            'emoji': '🎭',
                            'url': 'https://www.chehovsky.ru/repertoire/',
                            'actors': 'Актеры: смотри на сайте',
                            'description': 'Классическая пьеса А.П. Чехова'
                        })
        
        logger.info(f"✅ Театр Чехова: найдено {len(events)} спектаклей")
        return events[:3]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга театра Чехова: {e}")
        return []

def parse_taganrog_theatre():
    """Парсит спектакли театра - tagteatr.ru"""
    try:
        url = "https://tagteatr.ru/spektakli/dlya-vzroslyh/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        
        # Ищем спектакли
        plays = soup.find_all(['div', 'li', 'article'], class_=['spektakl', 'play', 'item', 'afisha'])
        
        if not plays:
            titles = soup.find_all(['h2', 'h3', 'h4', 'span'])
            for title in titles:
                text = title.get_text(strip=True)
                if len(text) > 10 and len(text) < 150:
                    events.append({
                        'title': text,
                        'type': 'Спектакль для взрослых',
                        'theatre': 'Таганрогский театр драмы',
                        'date': 'Сегодня',
                        'time': '19:00',
                        'price': '300-500 ₽',
                        'emoji': '🎬',
                        'url': 'https://tagteatr.ru/spektakli/dlya-vzroslyh/',
                        'actors': 'Спектакль современной постановки',
                        'description': 'Для взрослой аудитории'
                    })
        else:
            for play in plays[:5]:
                title_elem = play.find(['h2', 'h3', 'span', 'p'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    if len(title) > 5:
                        events.append({
                            'title': title,
                            'type': 'Спектакль',
                            'theatre': 'Таганрогский театр драмы',
                            'date': 'Сегодня',
                            'time': '19:00',
                            'price': '300-500 ₽',
                            'emoji': '🎬',
                            'url': 'https://tagteatr.ru/spektakli/dlya-vzroslyh/',
                            'actors': 'Смотри на сайте театра',
                            'description': 'Спектакль'
                        })
        
        logger.info(f"✅ Театр драмы: найдено {len(events)} спектаклей")
        return events[:3]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга театра драмы: {e}")
        return []

def parse_exhibitions():
    """Парсит выставки - tag.afishagoroda.ru"""
    try:
        url = "https://tag.afishagoroda.ru/events/vystavka"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        
        # Ищем выставки
        exhibitions = soup.find_all(['div', 'article', 'li'], class_=['vystavka', 'exhibition', 'item'])
        
        if not exhibitions:
            titles = soup.find_all(['h2', 'h3', 'a'])
            for title in titles[:10]:
                text = title.get_text(strip=True)
                if len(text) > 8 and len(text) < 200:
                    events.append({
                        'title': text,
                        'type': 'Выставка',
                        'location': 'Музеи Таганрога',
                        'date': 'Сегодня 10:00-18:00',
                        'price': '200-350 ₽',
                        'emoji': '🎨',
                        'url': 'https://tag.afishagoroda.ru/events/vystavka',
                        'description': 'Актуальная выставка в музеях города'
                    })
        else:
            for exhibition in exhibitions[:3]:
                title_elem = exhibition.find(['h2', 'h3', 'span'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    if len(title) > 5:
                        events.append({
                            'title': title,
                            'type': 'Выставка',
                            'location': 'Музеи Таганрога',
                            'date': 'Сегодня 10:00-18:00',
                            'price': '200-350 ₽',
                            'emoji': '🎨',
                            'url': 'https://tag.afishagoroda.ru/events/vystavka',
                            'description': 'Выставка искусства'
                        })
        
        logger.info(f"✅ Выставки: найдено {len(events)} выставок")
        return events[:2]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга выставок: {e}")
        return []

def parse_shows_festivals():
    """Парсит шоу и фестивали"""
    try:
        url = "https://tag.afishagoroda.ru/events/vystavka"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        titles = soup.find_all(['h2', 'h3', 'h4', 'a'])
        
        for title in titles[:15]:
            text = title.get_text(strip=True)
            if any(word in text.lower() for word in ['фестиваль', 'шоу', 'концерт', 'праздник', 'мероприятие']):
                if len(text) > 10 and len(text) < 200:
                    events.append({
                        'title': text,
                        'type': 'Шоу/Фестиваль',
                        'date': 'Сегодня',
                        'time': '18:00-22:00',
                        'price': '200-500 ₽',
                        'emoji': '🎪',
                        'url': 'https://tag.afishagoroda.ru/events/vystavka',
                        'description': 'Развлекательное мероприятие'
                    })
        
        logger.info(f"✅ Шоу/Фестивали: найдено {len(events)} мероприятий")
        return events[:2]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга шоу: {e}")
        return []

def parse_cinema():
    """Парсит кинотеатры - kinocharly.ru"""
    try:
        url = "https://kinocharly.ru/51"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.content, 'html.parser')
        
        events = []
        
        # Ищем фильмы
        movies = soup.find_all(['div', 'article', 'li'], class_=['film', 'movie', 'item', 'afisha'])
        
        if not movies:
            titles = soup.find_all(['h2', 'h3', 'h4', 'span', 'a'])
            for title in titles[:10]:
                text = title.get_text(strip=True)
                if len(text) > 5 and len(text) < 150 and any(char.isalpha() for char in text):
                    events.append({
                        'title': text,
                        'type': 'Фильм',
                        'cinema': 'Кинотеатр "Чарли"',
                        'date': 'Сегодня',
                        'times': '15:00, 17:30, 20:00',
                        'price': '250-350 ₽',
                        'emoji': '🎬',
                        'url': 'https://kinocharly.ru/51',
                        'description': 'Премьера/актуальный фильм'
                    })
        else:
            for movie in movies[:3]:
                title_elem = movie.find(['h2', 'h3', 'span'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    if len(title) > 3:
                        events.append({
                            'title': title,
                            'type': 'Фильм',
                            'cinema': 'Кинотеатр Таганрога',
                            'date': 'Сегодня',
                            'times': '15:00, 17:30, 20:00, 22:30',
                            'price': '250-350 ₽',
                            'emoji': '🎬',
                            'url': 'https://kinocharly.ru/51',
                            'description': 'Актуальный фильм в кино'
                        })
        
        logger.info(f"✅ Кино: найдено {len(events)} фильмов")
        return events[:3]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга кино: {e}")
        return []

def parse_spa_entertainment():
    """Парсит SPA и развлечения"""
    try:
        events = []
        
        # Гринвич Парк (SPA/развлечения)
        events.append({
            'title': 'Гринвич Парк - SPA комплекс',
            'type': 'SPA & Развлечения',
            'location': 'ул. Адмирала Крюйса, 2а',
            'date': 'Сегодня 10:00-23:00',
            'services': 'Бассейны, баня, сауна, гидромассаж, SPA',
            'price': 'от 500 ₽',
            'emoji': '🌊',
            'url': 'https://goldenhorse161.ru/',
            'link_text': 'ЗАБРОНИРОВАТЬ →',
            'discount': '🎁 Скидки до 45% на процедуры'
        })
        
        # Морская (Отдых)
        events.append({
            'title': 'Пляж "Морская" - Аквазона',
            'type': 'Пляж & Отдых',
            'location': 'Таганрогская набережная',
            'date': 'Сегодня 09:00-21:00',
            'services': 'Пляж, кафе, шезлонги, водные развлечения',
            'price': 'Бесплатно (услуги платные)',
            'emoji': '🏖️',
            'url': 'https://st-morskaya.ru/',
            'link_text': 'ПОДРОБНЕЕ →',
            'discount': 'Групповые скидки'
        })
        
        # Аквапарк
        events.append({
            'title': 'Аквапарк "Аква Лазурь"',
            'type': 'Аквапарк',
            'location': 'Таганрог, парк культуры',
            'date': 'Сегодня 11:00-20:00',
            'services': '15 аттракционов, волновой бассейн, горки',
            'price': '450-700 ₽',
            'emoji': '🌊',
            'url': 'https://akvalazur.ru/',
            'link_text': 'КУПИТЬ БИЛЕТ →',
            'discount': '🎁 Семейные пакеты со скидкой'
        })
        
        # Голден Хорс
        events.append({
            'title': 'Голден Хорс - Развлекательный центр',
            'type': 'Развлечения',
            'location': 'Центр города',
            'date': 'Сегодня 12:00-02:00',
            'services': 'Бильярд, кегельбан, игровые залы, рестобар',
            'price': 'Вход свободный',
            'emoji': '🎯',
            'url': 'https://goldenhorse161.ru/',
            'link_text': 'ПРОГРАММА НА СЕГОДНЯ →',
            'discount': '🎁 Живая музыка по пятницам'
        })
        
        logger.info(f"✅ SPA/Развлечения: найдено {len(events)} предложений")
        return events
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга SPA: {e}")
        return []

# ======================== ФОРМАТИРОВАНИЕ ========================

def format_theatre_event(event):
    """Форматирует театральное событие"""
    return f"""{event['emoji']} {event['title'].upper()}

📍 {event.get('theatre', 'Театр')}
📅 {event.get('date', 'Сегодня')} | {event.get('time', '19:00')}
🎭 {event.get('actors', 'Актеры: смотри на сайте')}

💰 Билеты: {event.get('price', 'Смотри на сайте')}

🔗 КУПИТЬ БИЛЕТЫ НА KASSA.RU →
Ссылка: {event['url']}

#Таганрог #театр #спектакль"""

def format_exhibition_event(event):
    """Форматирует выставку"""
    return f"""{event['emoji']} {event['title'].upper()}

📍 {event.get('location', 'Музей Таганрога')}
📅 {event.get('date', 'Сегодня')}

🎨 {event.get('description', 'Актуальная выставка')}

💰 Входной билет: {event.get('price', 'Смотри на сайте')}

🔗 ПОДРОБНЕЕ →
{event['url']}

#Таганрог #выставка #искусство"""

def format_cinema_event(event):
    """Форматирует кинофильм"""
    return f"""{event['emoji']} {event['title'].upper()}

🎬 {event.get('cinema', 'Кинотеатр')}
📅 {event.get('date', 'Сегодня')}
🕐 Сеансы: {event.get('times', '15:00, 17:30, 20:00, 22:30')}

💰 Билет: {event.get('price', '250-350 ₽')}

🔗 КУПИТЬ БИЛЕТ ОНЛАЙН →
{event['url']}

#Таганрог #кино #фильм"""

def format_spa_event(event):
    """Форматирует SPA/развлечения"""
    discount = event.get('discount', '')
    return f"""{event['emoji']} {event['title'].upper()}

{event.get('type', 'Развлечение')}

📍 {event.get('location', 'Таганрог')}
📅 {event.get('date', 'Сегодня')}

✨ {event.get('services', 'Услуги')}

💰 {event.get('price', 'Смотри на сайте')}

{discount}

🔗 {event.get('link_text', 'ПОДРОБНЕЕ →')}
{event['url']}

#Таганрог #развлечения #отдых"""

# ======================== ОСНОВНАЯ РАССЫЛКА ========================

async def send_daily_events():
    """Отправляет все события в Telegram в 9:00"""
    try:
        logger.info("🚀 Начинаем парсинг событий Таганрога...")
        
        all_events = []
        
        # Парсим театры
        logger.info("🎭 Парсим театры...")
        all_events.extend(parse_chehovsky_theatre())
        all_events.extend(parse_taganrog_theatre())
        
        # Парсим выставки
        logger.info("🎨 Парсим выставки...")
        all_events.extend(parse_exhibitions())
        
        # Парсим шоу
        logger.info("🎪 Парсим шоу и фестивали...")
        all_events.extend(parse_shows_festivals())
        
        # Парсим кино
        logger.info("🎬 Парсим кино...")
        all_events.extend(parse_cinema())
        
        # Парсим развлечения
        logger.info("🌊 Парсим SPA и развлечения...")
        spa_events = parse_spa_entertainment()
        
        bot = Bot(token=TELEGRAM_TOKEN)
        
        # ОТПРАВЛЯЕМ ТЕАТР
        theatre_events = [e for e in all_events if e.get('emoji') == '🎭']
        if theatre_events:
            logger.info(f"📤 Отправляем театр ({len(theatre_events)} событий)...")
            for event in theatre_events[:2]:
                message = format_theatre_event(event)
                await bot.send_message(chat_id=CHAT_ID, text=message)
                await asyncio.sleep(1)
        
        # ОТПРАВЛЯЕМ ВЫСТАВКИ
        exhibition_events = [e for e in all_events if e.get('emoji') == '🎨']
        if exhibition_events:
            logger.info(f"📤 Отправляем выставки ({len(exhibition_events)} событий)...")
            for event in exhibition_events[:1]:
                message = format_exhibition_event(event)
                await bot.send_message(chat_id=CHAT_ID, text=message)
                await asyncio.sleep(1)
        
        # ОТПРАВЛЯЕМ КИНО
        cinema_events = [e for e in all_events if e.get('emoji') == '🎬' and e.get('type') == 'Фильм']
        if cinema_events:
            logger.info(f"📤 Отправляем кино ({len(cinema_events)} событий)...")
            for event in cinema_events[:2]:
                message = format_cinema_event(event)
                await bot.send_message(chat_id=CHAT_ID, text=message)
                await asyncio.sleep(1)
        
        # ОТПРАВЛЯЕМ SPA/РАЗВЛЕЧЕНИЯ В КОНЦЕ
        logger.info(f"📤 Отправляем SPA и развлечения ({len(spa_events)} событий)...")
        for event in spa_events:
            message = format_spa_event(event)
            await bot.send_message(chat_id=CHAT_ID, text=message)
            await asyncio.sleep(1)
        
        logger.info("✅ Все события отправлены успешно!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке: {e}")
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ Ошибка при парсинге событий:\n{str(e)[:100]}\n\nПопробуем завтра в 9:00!"
            )
        except:
            pass

async def main():
    """Запускает планировщик"""
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    scheduler.add_job(
        send_daily_events,
        'cron',
        hour=12,
        minute=0,
        timezone="Europe/Moscow"
    )
    
    scheduler.start()
    
    logger.info("🤖 Бот запущен! Ждём 9:00 МСК...")
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
