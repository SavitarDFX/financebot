import ssl
import asyncio
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta
import nest_asyncio  # Для MacOS
from bs4 import BeautifulSoup
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.helpers import escape_markdown

ssl._create_default_https_context = ssl._create_unverified_context

TOKEN = "8000056252:AAH1BZNIygpX9f-weNLbhxfbB1dYiNVdMI4"
bot = Bot(token=TOKEN)
subscribed_users = set()

# Фиксируем event loop (только для MacOS)
nest_asyncio.apply()

# Подключение к MongoDB
client = MongoClient("mongodb://localhost:27017/")  # Указываем строку подключения к MongoDB
db = client["currency_db"]  # Создаем/используем базу данных
currency_collection = db["currency_rates"]  # Коллекция для хранения валютных курсов

# Функция получения курса валют с НБРБ
def get_currency_rates():
    currencies = ["USD", "EUR", "RUB", "CNY"]
    rates = {}

    for cur in currencies:
        url = f"https://api.nbrb.by/exrates/rates/{cur}?parammode=2"
        response = requests.get(url, verify=False)  # Отключаем проверку SSL
        print(f"[LOG] Запрос: {url} -> {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            sale_rate = data.get("Cur_OfficialRate", None)
            if sale_rate:
                rates[cur] = sale_rate  # Сохраняем только курс продажи
            else:
                rates[cur] = "N/A"
        else:
            rates[cur] = "N/A"

    return rates

# Функция сохранения курса валют в MongoDB
def save_currency_rate_to_db(rates):
    timestamp = datetime.now()
    for cur, rate in rates.items():
        if rate != "N/A":
            currency_collection.insert_one({
                "currency": cur,
                "rate": rate,
                "timestamp": timestamp
            })
        else:
            print(f"[LOG] Нет данных по {cur}")

# Функция для вычисления динамики курса валют за текущий день
def get_currency_dynamic(currency):
    # Получаем курсы валют за сегодняшний день
    start_of_day = datetime.combine(datetime.today(), datetime.min.time())  # Начало дня (00:00)
    end_of_day = datetime.now()  # Конец дня (текущий момент)

    # Ищем курсы валют за сегодняшний день
    pipeline = [
        {"$match": {"currency": currency, "timestamp": {"$gte": start_of_day, "$lte": end_of_day}}},
        {"$sort": {"timestamp": 1}},  # Сортировка по времени от старых к новым
        {"$project": {"rate": 1, "timestamp": 1}}
    ]
    rates = list(currency_collection.aggregate(pipeline))

    # Если данных нет, возвращаем сообщение
    if len(rates) == 0:
        return f"Нет данных за сегодняшний день."

    # Если данных меньше, чем нужно для расчета, то выводим +0% и +0 BYN
    first_rate = rates[0]['rate']
    last_rate = rates[-1]['rate']

    change_byn = last_rate - first_rate
    change_percent = (change_byn / first_rate) * 100

    return f"{change_byn:+.2f} BYN ({change_percent:+.2f}%) за сегодняшний день."

# Функция получения ЗВР
def get_reserve_assets():
    url = "https://www.nbrb.by/statistics/reserveassets/assets"
    response = requests.get(url, verify=False)  # Отключаем проверку SSL
    print(f"[LOG] Запрос: {url} -> {response.status_code}")

    if response.status_code != 200:
        return "⚠ Не удалось получить данные о ЗВР."

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tbody tr")

    if len(rows) < 5:
        return "⚠ Недостаточно данных о ЗВР."

    latest_date = rows[0].select_one("td").text.strip()
    latest_value = float(rows[0].select("td")[1].text.replace("\xa0", "").replace(",", "."))

    old_value = float(rows[4].select("td")[1].text.replace("\xa0", "").replace(",", "."))

    percent_change = ((latest_value - old_value) / old_value) * 100

    return f"📅 На {latest_date} объем ЗВР: {latest_value:.1f} млн USD ({percent_change:+.2f}% за 4 месяца)"

# Функция для получения бензина
def get_ai95_prices():
    url = "https://autotraveler.ru/belarus/dinamika-izmenenija-cen-na-benzin-v-belarusi.html"
    response = requests.get(url)  # Используем сессию с обновленным SSL-контекстом
    print(f"[LOG] Запрос: {url} -> {response.status_code}")

    if response.status_code != 200:
        return "⚠ Не удалось получить данные о ценах на бензин."

    soup = BeautifulSoup(response.text, "html.parser")

    # Находим элемент перед таблицей, который служит ориентиром
    local_header = soup.find("h2", {"id": "local"})
    if not local_header:
        return "⚠ Не найден элемент с заголовком 'Динамика изменения цен на бензин'."

    # Находим саму таблицу с ценами на бензин
    table = local_header.find_next("table", class_="table table-bordered table-hover")
    if not table:
        return "⚠ Не найдена таблица с данными по бензину."

    rows = table.find_all("tr")[1:]  # Пропускаем заголовок таблицы
    ai95_data = {}

    # Парсим данные по бензину 95
    for row in rows:
        cols = row.find_all("td")
        fuel_type = cols[0].text.strip()
        
        if fuel_type == "Бензин 95":
            current_price = cols[1].text.strip().replace("BYN", "").strip()

            # Извлекаем данные по месячному и годовому изменению
            month_change = cols[3].text.strip().split("\n")[0].replace("BYN", "").strip()
            month_percentage = cols[3].find("sub").text.strip().replace("%", "").strip()

            year_change = cols[4].text.strip().split("\n")[0].replace("BYN", "").strip()
            year_percentage = cols[4].find("sub").text.strip().replace("%", "").strip()

            ai95_data = {
                "current_price": current_price,
                "month_change": month_change,
                "month_percentage": month_percentage,
                "year_change": year_change,
                "year_percentage": year_percentage
            }

    if not ai95_data:
        return "⚠ Не найдены данные по бензину 95."

    current_price = ai95_data['current_price']
    month_change = ai95_data['month_change']
    month_percentage = ai95_data['month_percentage']
    year_change = ai95_data['year_change']
    year_percentage = ai95_data['year_percentage']

    # Исправляем форматирование, убираем % в неправильных местах
    return f"⛽ Бензин 95:\nЦена: {current_price} BYN\nДинамика за месяц: {month_change} BYN ({month_percentage}%)\nДинамика за год: {year_change} BYN ({year_percentage}%)"

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    subscribed_users.add(user_id)
    print(f"[LOG] Пользователь {user_id} подписался")

    keyboard = [["📊 Получить сводку"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(
        "Привет! Ты подписан на экономические сводки.\nНажми кнопку ниже, чтобы получить курс валют.",
        reply_markup=reply_markup
    )

# Обработчик кнопки "📊 Получить сводку"
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📊 Получить сводку":
        await report(update, context)

# Команда /report
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    print(f"[LOG] Пользователь {user_id} запросил /report")

    rates = get_currency_rates()  # Получаем только курсы продажи
    save_currency_rate_to_db(rates)  # Сохраняем курс в базу
    usd_dynamic = get_currency_dynamic("USD")
    eur_dynamic = get_currency_dynamic("EUR")
    cny_dynamic = get_currency_dynamic("CNY")
    rub_dynamic = get_currency_dynamic("RUB")
    zvr_data = get_reserve_assets()
    ai95_price = get_ai95_prices()

    message = f"📊 *Курсы валют (НБРБ):*\n"
    message += f"💵 USD/BYN: {rates.get('USD', 'N/A')} ({usd_dynamic})\n"
    message += f"💶 EUR/BYN: {rates.get('EUR', 'N/A')} ({eur_dynamic})\n"
    message += f"🇨🇳 CNY/BYN: {rates.get('CNY', 'N/A')} ({cny_dynamic})\n"
    message += f"🇷🇺 RUB/BYN: {rates.get('RUB', 'N/A')} ({rub_dynamic})\n\n"
    message += f"🏦 *Золотовалютные резервы:*\n{zvr_data}\n\n"
    message += f"{ai95_price}"

    message = escape_markdown(message, version=2)

    await update.message.reply_text(message, parse_mode="MarkdownV2")

# Функция для автоматических уведомлений
async def send_daily_updates():
    rates = get_currency_rates()
    save_currency_rate_to_db(rates)
    usd_dynamic = get_currency_dynamic("USD")
    eur_dynamic = get_currency_dynamic("EUR")
    cny_dynamic = get_currency_dynamic("CNY")
    rub_dynamic = get_currency_dynamic("RUB")
    zvr_data = get_reserve_assets()
    ai95_price = get_ai95_prices()

    message = f"📊 *Курсы валют (НБРБ):*\n"
    message += f"💵 USD/BYN: {rates.get('USD', 'N/A')} ({usd_dynamic})\n"
    message += f"💶 EUR/BYN: {rates.get('EUR', 'N/A')} ({eur_dynamic})\n"
    message += f"🇨🇳 CNY/BYN: {rates.get('CNY', 'N/A')} ({cny_dynamic})\n"
    message += f"🇷🇺 RUB/BYN: {rates.get('RUB', 'N/A')} ({rub_dynamic})\n\n"
    message += f"🏦 *Золотовалютные резервы:*\n{zvr_data}\n\n"
    message += f"{ai95_price}"

    message = escape_markdown(message, version=2)

    # Отправка уведомлений всем подписанным пользователям
    for user_id in subscribed_users:
        try:
            await bot.send_message(user_id, message, parse_mode="MarkdownV2")
        except Exception as e:
            print(f"[ERROR] Ошибка при отправке сообщения пользователю {user_id}: {e}")

# Настройка расписания для уведомлений
async def schedule_updates():
    while True:
        now = datetime.now()
        if now.hour in [9, 12, 15, 18, 21, 24]:
            await send_daily_updates()
        await asyncio.sleep(3600)  # Пауза на 1 час

# Основная функция для запуска бота
async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запуск расписания обновлений
    asyncio.create_task(schedule_updates())

    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
