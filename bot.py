import ssl
import asyncio
import requests
import sqlite3
from sqlite3 import Error
from datetime import datetime, timedelta
import nest_asyncio
from bs4 import BeautifulSoup
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import pytz

try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

TOKEN = "8000056252:AAH1BZNIygpX9f-weNLbhxfbB1dYiNVdMI4"
bot = Bot(token=TOKEN)
subscribed_users = set()

try:
    nest_asyncio.apply()
except RuntimeError:
    pass


def create_connection():
    conn = None
    try:
        conn = sqlite3.connect("finance_bot.db")
        return conn
    except Error as e:
        print(e)
    return conn


def create_tables(conn):
    try:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS currency_rates
                     (id INTEGER PRIMARY KEY, currency TEXT, rate REAL, timestamp TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS zvr_data
                     (id INTEGER PRIMARY KEY, value REAL, timestamp TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai95_prices
                     (id INTEGER PRIMARY KEY, price REAL, timestamp TEXT)"""
        )
        conn.commit()
    except Error as e:
        print(e)


def get_currency_rate(currency):
    url = f"https://api.nbrb.by/exrates/rates/{currency}?parammode=2"
    response = requests.get(url, verify=False)
    print(f"[LOG] Запрос: {url} -> {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        return data.get("Cur_OfficialRate", "N/A")
    else:
        return "N/A"


def get_currency_rates():
    currencies = ["USD", "EUR", "RUB", "CNY"]
    return {cur: get_currency_rate(cur) for cur in currencies}


def save_currency_rate_to_db(conn, rates):
    timestamp = datetime.now(pytz.timezone("Europe/Minsk")).isoformat()
    c = conn.cursor()
    for cur, rate in rates.items():
        if rate != "N/A":
            c.execute(
                "INSERT INTO currency_rates (currency, rate, timestamp) VALUES (?, ?, ?)",
                (cur, float(rate), timestamp),
            )
    conn.commit()


def save_zvr_to_db(conn, zvr_value):
    if zvr_value is not None:
        timestamp = datetime.now(pytz.timezone("Europe/Minsk")).isoformat()
        c = conn.cursor()
        c.execute(
            "INSERT INTO zvr_data (value, timestamp) VALUES (?, ?)",
            (zvr_value, timestamp),
        )
        conn.commit()
    else:
        print("[LOG] Нет данных по ЗВР для сохранения")


def save_ai95_to_db(conn, price_value):
    if price_value is not None:
        timestamp = datetime.now(pytz.timezone("Europe/Minsk")).isoformat()
        c = conn.cursor()
        c.execute(
            "INSERT INTO ai95_prices (price, timestamp) VALUES (?, ?)",
            (price_value, timestamp),
        )
        conn.commit()
    else:
        print("[LOG] Нет данных по ценам на бензин для сохранения")


def get_dynamics(conn):
    now = datetime.now(pytz.timezone("Europe/Minsk"))
    month_ago = (now - timedelta(days=30)).isoformat()

    dynamics = {}
    time_range = "0 дней"

    c = conn.cursor()

    for currency in ["USD", "EUR", "RUB", "CNY"]:
        c.execute(
            "SELECT rate, timestamp FROM currency_rates WHERE currency = ? ORDER BY timestamp DESC LIMIT 1",
            (currency,),
        )
        current = c.fetchone()
        c.execute(
            "SELECT rate, timestamp FROM currency_rates WHERE currency = ? AND timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (currency, month_ago),
        )
        oldest = c.fetchone()

        if current and oldest:
            current_rate, current_timestamp = current
            oldest_rate, oldest_timestamp = oldest
            time_diff = datetime.fromisoformat(
                current_timestamp
            ) - datetime.fromisoformat(oldest_timestamp)
            time_range = max(time_range, f"{time_diff.days} дней")

            change = current_rate - oldest_rate
            percent_change = (change / oldest_rate) * 100
            dynamics[f"{currency}_BYN"] = f"{change:+.4f} {percent_change:+.2f}%"

    for table, key in [("zvr_data", "value"), ("ai95_prices", "price")]:
        c.execute(
            f"SELECT {key}, timestamp FROM {table} ORDER BY timestamp DESC LIMIT 1"
        )
        current = c.fetchone()
        c.execute(
            f"SELECT {key}, timestamp FROM {table} WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (month_ago,),
        )
        oldest = c.fetchone()

        if current and oldest:
            current_value, current_timestamp = current
            oldest_value, oldest_timestamp = oldest
            time_diff = datetime.fromisoformat(
                current_timestamp
            ) - datetime.fromisoformat(oldest_timestamp)
            time_range = max(time_range, f"{time_diff.days} дней")

            change = current_value - oldest_value
            percent_change = (change / oldest_value) * 100
            dynamics[table] = f"{change:+.2f} {percent_change:+.2f}%"

    return dynamics, time_range


def analyze_dynamics(dynamics):
    max_deviation = max(
        abs(float(d.split()[1].strip("()%"))) for d in dynamics.values()
    )

    if max_deviation > 10:
        return "🚨 Требуется срочное вмешательство"
    elif max_deviation > 5:
        return "⚠️ Лучше просмотреть котировку"
    else:
        return "✅ Все нормально, вмешательств не требуется"


def get_reserve_assets():
    url = "https://www.nbrb.by/statistics/reserveassets/assets"
    response = requests.get(url, verify=False)
    print(f"[LOG] Запрос: {url} -> {response.status_code}")

    if response.status_code != 200:
        return "⚠ Не удалось получить данные о ЗВР.", None

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tbody tr")

    if len(rows) < 5:
        return "⚠ Недостаточно данных о ЗВР.", None

    latest_date = rows[0].select_one("td").text.strip()
    latest_value = float(
        rows[0].select("td")[1].text.replace("\xa0", "").replace(",", ".")
    )

    if len(rows) >= 5:
        four_months_ago_value = float(
            rows[4].select("td")[1].text.replace("\xa0", "").replace(",", ".")
        )
        percent_change = (
            (latest_value - four_months_ago_value) / four_months_ago_value
        ) * 100
        change_sign = "+" if percent_change >= 0 else ""
    else:
        change_sign = ""
        percent_change = 0

    return (
        f"📅 На {latest_date} объем ЗВР: <b>{latest_value:.1f} млн USD</b> ({change_sign}{percent_change:.1f}%)",
        latest_value,
    )


def get_ai95_prices():
    url = "https://autotraveler.ru/belarus/dinamika-izmenenija-cen-na-benzin-v-belarusi.html"
    response = requests.get(url)
    print(f"[LOG] Запрос: {url} -> {response.status_code}")

    if response.status_code != 200:
        return "⚠ Не удалось получить данные о ценах на бензин.", None

    soup = BeautifulSoup(response.text, "html.parser")
    local_header = soup.find("h2", {"id": "local"})
    if not local_header:
        return (
            "⚠ Не найден элемент с заголовком 'Динамика изменения цен на бензин'.",
            None,
        )

    table = local_header.find_next("table", class_="table table-bordered table-hover")
    if not table:
        return "⚠ Не найдена таблица с данными по бензину.", None

    rows = table.find_all("tr")[1:]
    for row in rows:
        cols = row.find_all("td")
        if cols[0].text.strip() == "Бензин 95":
            current_price = cols[1].text.strip().replace("BYN", "").strip()
            return current_price, float(current_price.replace("*", ""))

    return "⚠ Не найдены данные по бензину 95.", None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    subscribed_users.add(user_id)
    print(f"[LOG] Пользователь {user_id} подписался")

    keyboard = [["📊 Получить сводку"]]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=False
    )

    await update.message.reply_text(
        "Привет!\nНажми кнопку ниже, чтобы получить экономическую сводку.",
        reply_markup=reply_markup,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📊 Получить сводку":
        await report(update, context)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id

    message = await update.message.reply_text("Отправлен запрос...")

    conn = create_connection()

    currencies = ["USD", "EUR", "RUB", "CNY"]
    rates = {}
    status_message = ""
    for cur in currencies:
        rate = get_currency_rate(cur)
        rates[cur] = rate
        status_message += (
            f"Запрос {cur}/BYN (НацБанк) - {'Успешно' if rate != 'N/A' else 'Ошибка'}\n"
        )
        await message.edit_text(f"Отправлен запрос...\n\n{status_message}")
    save_currency_rate_to_db(conn, rates)

    zvr_data, zvr_value = get_reserve_assets()
    save_zvr_to_db(conn, zvr_value)
    status_message += (
        f"Запрос ЗВР (НацБанк) - {'Успешно' if zvr_value is not None else 'Ошибка'}\n"
    )
    await message.edit_text(f"Отправлен запрос...\n\n{status_message}")

    ai95_price, ai95_value = get_ai95_prices()
    save_ai95_to_db(conn, ai95_value)
    status_message += (
        f"Запрос Бензин 95 - {'Успешно' if ai95_value is not None else 'Ошибка'}\n"
    )
    await message.edit_text(f"Отправлен запрос...\n\n{status_message}")

    dynamics, time_range = get_dynamics(conn)
    status = analyze_dynamics(dynamics)

    final_message = f"<b>{status}</b>\n\n"
    final_message += f"📊 <b><u>Курсы валют (НБРБ):</u></b>\n"
    for currency in currencies:
        rate = rates.get(currency, "N/A")
        dynamic = dynamics.get(f"{currency}_BYN", "")
        if rate != "N/A" and dynamic:
            change_byn, percent = dynamic.split(" ", 1)
            change_byn = change_byn.replace("BYN", "").strip()
            final_message += f"<b>{currency}/BYN:</b> {rate} BYN \n <b>Динамика:</b> {change_byn}р, {percent}\n\n"
        else:
            final_message += f"<b>{currency}/BYN:</b> {rate}\n"
    final_message += f"\n🏦 <b><u>Золотовалютные резервы:</u></b>\n{zvr_data}\n\n"
    final_message += f"⛽ <b><u>Бензин 95:</u></b>\n<b>Цена:</b> {ai95_value} BYN\n\n"
    final_message += f"Динамика составлена за: {time_range}"

    await message.edit_text(final_message, parse_mode="HTML")

    conn.close()


async def send_updates(force=False):
    conn = create_connection()
    rates = get_currency_rates()
    save_currency_rate_to_db(conn, rates)
    zvr_data, zvr_value = get_reserve_assets()
    save_zvr_to_db(conn, zvr_value)
    ai95_price, ai95_value = get_ai95_prices()
    save_ai95_to_db(conn, ai95_value)

    dynamics, time_range = get_dynamics(conn)
    status = analyze_dynamics(dynamics)

    if force or status != "✅ Все нормально, вмешательств не требуется":
        message = f"<b>{status}</b>\n\n"
        message += f"📊 <b><u>Курсы валют (НБРБ):</u></b>\n"
        for currency in ["USD", "EUR", "RUB", "CNY"]:
            rate = rates.get(currency, "N/A")
            dynamic = dynamics.get(f"{currency}_BYN", "")
            if rate != "N/A" and dynamic:
                change_byn, percent = dynamic.split(" ", 1)
                change_byn = change_byn.replace("BYN", "").strip()
                message += f"<b>{currency}/BYN:</b> {rate} BYN \n <b>Динамика:</b> {change_byn}р, {percent}\n\n"
            else:
                message += f"<b>{currency}/BYN:</b> {rate}\n"
        message += f"\n🏦 <b><u>Золотовалютные резервы:</u></b>\n{zvr_data}\n\n"
        message += f"⛽ <b><u>Бензин 95:</u></b>\n<b>Цена:</b> {ai95_value} BYN\n\n"
        message += f"<b>Динамика составлена за:</b> {time_range}"

        for user_id in subscribed_users:
            try:
                await bot.send_message(user_id, message, parse_mode="HTML")
            except Exception as e:
                print(
                    f"[ERROR] Ошибка при отправке сообщения пользователю {user_id}: {e}"
                )

    conn.close()


async def schedule_updates():
    while True:
        now = datetime.now(pytz.timezone("Europe/Minsk"))
        if now.hour in [9, 12, 15, 18, 21, 0] and now.minute == 0:
            await send_updates(force=True)
        elif now.minute % 30 == 0:
            await send_updates()
        await asyncio.sleep(60)


async def main():
    conn = create_connection()
    if conn is not None:
        create_tables(conn)
    else:
        print("Error! Cannot create the database connection.")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    asyncio.create_task(schedule_updates())

    await application.run_polling()

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
