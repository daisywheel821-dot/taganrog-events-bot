import asyncio
import aiohttp
import os
from telegram import Bot

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHANNEL_ID") or os.getenv("CHAT_ID")

async def test_all():
    print("--- 1. Проверка Telegram Бота ---")
    print(f"BOT_TOKEN существует? {bool(BOT_TOKEN)}")
    print(f"CHAT_ID существует? {bool(CHAT_ID)}")

    if not BOT_TOKEN or not CHAT_ID:
        print("❌ ОШИБКА: Переменные BOT_TOKEN или CHAT_ID не найдены в Secrets GitHub!")
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        print(f"✅ Бот авторизован: @{me.username}")
        await bot.send_message(chat_id=CHAT_ID, text="🤖 Тест из GitHub Actions: проверка связи прошла успешно!")
        print("✅ Тестовое сообщение успешно отправлено в Telegram-канал!")
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")

    print("\n--- 2. Проверка сайтов Таганрога ---")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for url in ["https://tgliamz.ru/calendar/", "https://www.chehovsky.ru/afishateatra/"]:
            try:
                async with session.get(url, timeout=10) as resp:
                    print(f"Сайт {url} -> Статус: {resp.status}")
            except Exception as e:
                print(f"❌ Ошибка подключения к {url}: {e}")

if __name__ == "__main__":
    asyncio.run(test_all())
