import re
import logging
import sqlite3
import os
import gspread
import asyncio
import json
from datetime import datetime, date, time, timedelta
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
)

# --- CONFIGURATION ---
TELEGRAM_TOKEN = ""
GOOGLE_SHEET_NAME = ""
GOOGLE_CREDS_FILE = ""
DATABASE_FILE = "db.db"
PRICES_WORKSHEET_NAME = "Prices"  # The tab with job names and prices
RESTRICTED_SHEET_CREATION = True  # If True, doesn't allow users to create sheets

# --- BUTTONS ---
REPLY_KEYBOARD = [
    ["Мій дохід (Місяць)", "Моя Робота"],
    ["Список Робіт", "Скасувати Останній"],
]

# --- FORMAT & MESSAGES ---
NEW_FORMAT_SPECIFIER = """
Вітаю! Я бот для обліку.
Використовуйте кнопки внизу для швидкого доступу до функцій.
/help для довідки.
"""
HELP_MESSAGE = """
**📖 Довідка по командам:**

**Основні команди:**
`/start` - Початок роботи та головне меню
`/setuser Ім'я` - Зареєструвати або оновити ваше ім'я
`/help` - Показати цю довідку

**Робота з даними:**
`/myincome` - Дохід за поточний місяць
`/myincome all` - Загальний дохід
`/myincome НазваПартії` - Дохід по конкретній партії
`/getjobs` - Список доступних робіт
`/mywork` - Останні 10 записів
`/mywork НазваПартії` - Записи по конкретній партії
`/rollback` - Скасувати останній запис

**Формат введення даних:**
`Партія 101: Збірка - 5, Пайка - 3;`
`Проект Альфа: Тест - 10;`
"""


# --- DATABASE FUNCTIONS ---
def db_connect():
    """Create a database connection to the SQLite database."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        return conn
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}")
    return conn


def setup_database():
    """Create the users table if it doesn't exist."""
    sql_create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    );
    """
    sql_create_submissions_table = """
    CREATE TABLE IF NOT EXISTS submissions (
        submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        timestamp DATETIME NOT NULL,
        party_name TEXT NOT NULL,
        work_items_json TEXT NOT NULL,
        FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
    );
    """
    sql_create_worksheets_table = """
    CREATE TABLE IF NOT EXISTS worksheets (
        party_name TEXT PRIMARY KEY,
        creation_date DATE NOT NULL
    );
    """
    conn = db_connect()
    if conn:
        try:
            c = conn.cursor()
            c.execute(sql_create_users_table)
            c.execute(sql_create_submissions_table)
            c.execute(sql_create_worksheets_table)
            conn.commit()
            conn.close()
            logger.info(
                "Database tables 'users', 'submissions', and 'worksheets' are ready."
            )
        except sqlite3.Error as e:
            logger.error(f"Database setup error: {e}")


def get_user_name(telegram_id: int) -> str | None:
    """Fetches the user's name from the database."""
    conn = db_connect()
    if conn:
        try:
            sql = "SELECT name FROM users WHERE telegram_id = ?;"
            cur = conn.cursor()
            cur.execute(sql, (telegram_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                return row[0]
        except sqlite3.Error as e:
            logger.error(f"Error fetching user: {e}")
    return None


def get_all_users() -> list[tuple[int, str]]:
    """Fetches all registered users (id, name) from the database."""
    conn = db_connect()
    if conn:
        try:
            sql = "SELECT telegram_id, name FROM users;"
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            conn.close()
            return rows
        except sqlite3.Error as e:
            logger.error(f"Error fetching all users: {e}")
    return []


# --- GOOGLE SHEETS FUNCTIONS ---
def get_gspread_client():
    """Authenticates and returns the gspread client using google-auth."""
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scope)
        client = gspread.authorize(creds)
        return client
    except FileNotFoundError:
        logger.error(
            f"Failed to connect to Google Sheets: {GOOGLE_CREDS_FILE} not found."
        )
        return None
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheets: {e}")
        return None


def load_prices(client) -> dict[str, float] | None:
    """Loads prices from the 'Prices' worksheet."""
    try:
        spreadsheet = client.open(GOOGLE_SHEET_NAME)
        prices_sheet = spreadsheet.worksheet(PRICES_WORKSHEET_NAME)
        rows = prices_sheet.get_all_values()[1:]  # Get all values, skip header row [1:]
        prices = {}
        for row in rows:
            if row[0] and row[1]:
                try:
                    price = float(row[1].replace(",", "."))
                    prices[row[0].strip()] = price
                except ValueError:
                    logger.warning(f"Invalid price for {row[0]}: {row[1]}")
        if not prices:
            logger.error(
                f"No prices loaded from '{PRICES_WORKSHEET_NAME}'. Sheet is empty or misformatted."
            )
            return None
        logger.info(f"Loaded prices: {prices}")
        return prices
    except WorksheetNotFound:
        logger.error(f"CRITICAL: Worksheet '{PRICES_WORKSHEET_NAME}' not found.")
        return None
    except Exception as e:
        logger.error(f"Failed to load prices: {e}")
        return None


def setup_party_worksheet(worksheet, prices: dict):
    """Creates the header rows for a new party worksheet."""
    headers = ["Ім'я"]
    for job_name in prices.keys():
        headers.append(f"{job_name} - К-сть")
    worksheet.append_row(headers)
    header_range = f"A1:{chr(64 + len(headers))}1"
    worksheet.format(header_range, {"textFormat": {"bold": True}})
    logger.info(
        f"Created headers for worksheet '{worksheet.title}' (v2 - no sum columns)"
    )


def to_float(value: str | None) -> float:
    """Safely convert sheet value (str, None) to float."""
    if not value:
        return 0.0
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return 0.0


def check_worksheet_exists(party_name: str) -> bool:
    """Blocking function to check if a worksheet exists."""
    client = get_gspread_client()
    if not client:
        logger.error("check_worksheet_exists: Failed to connect to GSpread.")
        return False
    try:
        spreadsheet = client.open(GOOGLE_SHEET_NAME)
        spreadsheet.worksheet(party_name)
        logger.info(f"Worksheet '{party_name}' check: Found.")
        return True
    except WorksheetNotFound:
        logger.info(f"Worksheet '{party_name}' check: Not found.")
        return False
    except Exception as e:
        logger.error(f"Failed to check worksheet existence: {e}")
        return False


async def save_submission_to_db(
    parsed_data: dict, telegram_id: int, context: CallbackContext
) -> bool:
    """Saves a submission record to the local database for rollback."""
    conn = db_connect()
    if conn:
        try:
            sql = """
            INSERT INTO submissions (telegram_id, timestamp, party_name, work_items_json)
            VALUES (?, ?, ?, ?);
            """
            work_items_str = json.dumps(parsed_data["work_items"], ensure_ascii=False)
            cur = conn.cursor()
            cur.execute(
                sql,
                (
                    telegram_id,
                    datetime.now().isoformat(),
                    parsed_data["party_name"],
                    work_items_str,
                ),
            )
            conn.commit()
            conn.close()
            logger.info(f"Saved submission to DB for user {telegram_id}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Failed to save submission to DB: {e}")
            await context.bot.send_message(
                chat_id=telegram_id,
                text="❌ Помилка: Не вдалося зберегти запис для відкату. "
                "Оновлення Google Sheets скасовано.",
            )
            return False
    return False


# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- TELEGRAM BOT ASYNC FUNCTIONS ---
async def start(update: Update, context: CallbackContext) -> None:
    """Send the format specifier message and show the main keyboard."""
    reply_markup = ReplyKeyboardMarkup(REPLY_KEYBOARD, resize_keyboard=True)
    await update.message.reply_text(
        NEW_FORMAT_SPECIFIER,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    """Show help message with all commands."""
    await update.message.reply_text(
        HELP_MESSAGE,
        parse_mode=ParseMode.MARKDOWN,
    )


async def set_user(update: Update, context: CallbackContext) -> None:
    """Save or update the user's name mapped to their Telegram ID."""
    telegram_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text(
            "Будь ласка, вкажіть ім'я. Приклад: `/setuser Іван`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    name = " ".join(context.args)
    conn = db_connect()
    if conn:
        try:
            sql = """
            INSERT INTO users(telegram_id, name) VALUES(?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name;
            """
            cur = conn.cursor()
            cur.execute(sql, (telegram_id, name))
            conn.commit()
            conn.close()
            logger.info(f"User {telegram_id} set name to '{name}'")
            await update.message.reply_text(f"✅ Вас зареєстровано як: {name}")
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"❌ Помилка: Ім'я '{name}' вже використовується кимось іншим."
            )
        except sqlite3.Error as e:
            logger.error(f"Failed to set user: {e}")
            await update.message.reply_text("❌ Сталася помилка бази даних.")


async def handle_message(update: Update, context: CallbackContext) -> None:
    """
    Handles text messages. Routes to button commands or parses submission format.
    """
    if not update.message or not update.message.text:
        return

    telegram_id = update.message.from_user.id
    message_text = update.message.text.strip()

    # Validation 1: User Registration
    user_name = get_user_name(telegram_id)
    if not user_name:
        logger.warning(f"Message from unregistered user {telegram_id}")
        await update.message.reply_text(
            "❌ Ви не зареєстровані.\n" "Будь ласка, використайте /setuser {Ваше Ім'я}."
        )
        return

    # Button Command Handling
    if message_text == "Мій дохід (Місяць)":
        context.args = []  # /myincome (без аргументів)
        await get_my_income(update, context)
        return
    elif message_text == "Моя Робота":
        context.args = []
        await get_my_work(update, context)
        return
    elif message_text == "Список Робіт":
        await get_jobs(update, context)
        return
    elif message_text == "Скасувати Останній":
        await rollback(update, context)
        return

    # Format parsing starts here
    match = re.match(r"^([^:]+):(.+);$", message_text, re.S)
    if not match:
        logger.warning(f"Invalid format from {user_name}: {message_text}")
        await update.message.reply_text(
            "❌ Помилка: Загальний формат невірний.\n"
            "Причина: Пропущено `:` або `;` в кінці.\n"
            "Формат: `Назва Партії: Робота 1 - Кількість 1, ... ;`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    party_name = match.group(1).strip()
    payload_str = match.group(2).strip()
    work_items = []

    # Validation 2: Party Name
    items_list = payload_str.split(",")
    for item_str in items_list:
        item_str_clean = item_str.strip()
        if not item_str_clean:
            continue
        parts = item_str_clean.split("-")
        if len(parts) != 2:
            await update.message.reply_text(
                f"❌ Помилка: Невірний формат роботи.\n"
                f"Причина: Порушення в: `{item_str_clean}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        action = parts[0].strip()
        amount_str = parts[1].strip()
        if not action or not amount_str:
            await update.message.reply_text(
                f"❌ Помилка: Пуста назва роботи або кількість.\n"
                f"Причина: `{item_str_clean}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            amount = float(amount_str.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                f"❌ Помилка: Кількість повинна бути числом.\n"
                f"Причина: {amount_str} не є числом в `{item_str_clean}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        work_items.append({"action": action, "amount": amount})

    if not work_items:
        await update.message.reply_text(
            "❌ Помилка: Не знайдено жодної роботи.\n"
            "Причина: Частина після : пуста або невірно відформатована."
        )
        return

    parsed_data = {
        "user_name": user_name,
        "party_name": party_name,
        "work_items": work_items,
    }
    logger.info(f"Successfully parsed data from {user_name}: {parsed_data}")

    #
    exists = await asyncio.to_thread(check_worksheet_exists, party_name)

    if exists:
        success = await save_submission_to_db(parsed_data, telegram_id, context)
        if success:
            result_message = await asyncio.to_thread(
                process_sheets_update, data=parsed_data
            )
            if result_message:
                await update.message.reply_text(
                    result_message, parse_mode=ParseMode.MARKDOWN
                )
    else:
        if RESTRICTED_SHEET_CREATION:
            await update.message.reply_text(
                f"❌ Аркуш з назвою `{party_name}` не знайдено, і створення нових аркушів заборонено.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        context.user_data["pending_submission"] = parsed_data
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Так, Створити", callback_data=f"create_ws_yes"
                ),
                InlineKeyboardButton("❌ Ні, Скасувати", callback_data="create_ws_no"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"⚠️ Аркуш з назвою `{party_name}` не знайдено.\n"
            f"Бажаєте створити новий аркуш з цією назвою?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )


def process_sheets_update(data: dict) -> str | None:
    """
    The core logic for updating Google Sheets with parsed data.
    """
    user_name = data["user_name"]
    party_name = data["party_name"]
    work_items = data["work_items"]

    client = get_gspread_client()
    if not client:
        return "❌ Помилка: Не вдалося підключитися до Google Sheets."

    prices = load_prices(client)
    if not prices:
        return (
            f"❌ Помилка: Не вдалося завантажити прайс-лист.\n"
            f"Перевірте, чи існує аркуш `{PRICES_WORKSHEET_NAME}` і чи він не пустий."
        )

    valid_work_items = []
    for item in work_items:
        if item["action"] not in prices:
            return (
                f"❌ Помилка: Робота `{item['action']}` не знайдена у прайс-листі.\n"
                f"Будь ласка, перевірте назву або додайте її в аркуш `{PRICES_WORKSHEET_NAME}`."
            )
        if item["amount"] != 0:
            valid_work_items.append(item)
    if not valid_work_items:
        logger.info(
            "No valid work items to process (amount might be 0). Skipping update."
        )
        return (
            f"✅ Дані успішно оновлено для `{party_name}`.\n"
            f"Загальний дохід по цьому запису: 0.00 грн."
        )

    work_items = valid_work_items

    is_rollback = any(item["amount"] < 0 for item in work_items)

    try:
        spreadsheet = client.open(GOOGLE_SHEET_NAME)

        try:
            worksheet = spreadsheet.worksheet(party_name)
        except WorksheetNotFound:
            if is_rollback:
                logger.warning(f"Rollback failed: Worksheet '{party_name}' not found.")
                return f"❌ Помилка відкату: Аркуш '{party_name}' не знайдено."

            logger.info(f"Worksheet '{party_name}' not found. Creating...")
            worksheet = spreadsheet.add_worksheet(title=party_name, rows=100, cols=50)
            setup_party_worksheet(worksheet, prices)
            logger.info(f"Створено новий аркуш для: `{party_name}`.")

            conn = db_connect()
            if conn:
                try:
                    sql = "INSERT INTO worksheets (party_name, creation_date) VALUES (?, ?);"
                    cur = conn.cursor()
                    cur.execute(sql, (party_name, date.today()))
                    conn.commit()
                    conn.close()
                    logger.info(f"Saved creation date for worksheet '{party_name}'")
                except sqlite3.IntegrityError:
                    logger.warning(f"Worksheet '{party_name}' already in DB. Ignoring.")
                except sqlite3.Error as e:
                    logger.error(f"Failed to save worksheet creation date: {e}")

        try:
            user_cell = worksheet.find(user_name, in_column=1)
            user_row = user_cell.row
        except Exception:
            if is_rollback:
                logger.warning(
                    f"Rollback failed: User '{user_name}' not found in sheet '{party_name}'."
                )
                return f"❌ Помилка відкату: Користувач '{user_name}' не знайдений в '{party_name}'."

            logger.info(f"User '{user_name}' not found in sheet. Creating new row.")
            new_row_data = [user_name] + [0] * len(prices)
            worksheet.append_row(new_row_data)
            user_row = len(worksheet.get_all_values())

        header_row = worksheet.row_values(1)
        updates_to_batch = []
        total_income_for_entry = 0

        for item in work_items:
            action = item["action"]
            amount = item["amount"]

            if not is_rollback:
                cost = amount * prices[action]
                total_income_for_entry += cost

            try:
                col_k = header_row.index(f"{action} - К-сть") + 1
            except ValueError:
                return (
                    f"❌ Помилка: Аркуш `{party_name}` має застарілу структуру або в ньому"
                    f" відсутня колонка для `{action} - К-сть`."
                )

            current_k = to_float(worksheet.cell(user_row, col_k).value)

            if current_k + amount < 0:
                logger.warning(
                    f"Rollback blocked: Not enough quantity for '{action}' for user '{user_name}'."
                )
                return (
                    f"❌ Помилка відкату: Недостатньо кількості для `{action}`.\n"
                    f"Наявна к-сть: {current_k}, Спроба відняти: {abs(amount)}"
                )

            updates_to_batch.append(
                {
                    "range": gspread.utils.rowcol_to_a1(user_row, col_k),
                    "values": [[current_k + amount]],
                }
            )

        if updates_to_batch:
            worksheet.batch_update(updates_to_batch)

        logger.info(
            f"Successfully updated sheet '{party_name}' for user '{user_name}' (v2 logic)."
        )

        if is_rollback:
            return f"✅ Запис успішно скасовано для `{party_name}`."
        else:
            return (
                f"✅ Дані успішно оновлено для `{party_name}`.\n"
                f"Загальний дохід по цьому запису: {total_income_for_entry:.2f} грн."
            )

    except Exception as e:
        logger.error(f"Failed during sheets update: {e}")
        return f"❌ Сталася непередбачена помилка Google Sheets: {e}"

    return None


async def get_my_income(update: Update, context: CallbackContext) -> None:
    """
    Sums up user's income.
    """
    telegram_id = update.message.from_user.id
    user_name = get_user_name(telegram_id)

    if not user_name:
        await update.message.reply_text("❌ Ви не зареєстровані. /setuser {Ваше Ім'я}")
        return

    client = get_gspread_client()
    if not client:
        await update.message.reply_text(
            "❌ Помилка: Не вдалося підключитися до Google Sheets."
        )
        return

    target_party_str = " ".join(context.args).strip()
    target: str
    if not target_party_str:
        target = "month"
    elif target_party_str.lower() == "all":
        target = "all"
    else:
        target = target_party_str

    try:

        def blocking_income_check(u_name: str, u_target: str):
            """Run blocking gspread code in a separate thread"""
            prices = load_prices(client)
            if not prices:
                return (
                    None,
                    None,
                    f"❌ Помилка: Не вдалося завантажити прайс-лист `{PRICES_WORKSHEET_NAME}`.",
                )

            spreadsheet = client.open(GOOGLE_SHEET_NAME)
            total_income = 0.0
            party_count = 0
            worksheets_to_check = []
            report_title = ""

            if u_target == "all":
                worksheets_to_check = [
                    ws
                    for ws in spreadsheet.worksheets()
                    if ws.title != PRICES_WORKSHEET_NAME
                ]
                report_title = "загальний дохід"

            elif u_target == "month":
                report_title = "дохід за поточний місяць"
                current_month_str = date.today().strftime("%Y-%m")
                conn = db_connect()
                party_names_from_db = []
                if conn:
                    try:
                        sql = "SELECT party_name FROM worksheets WHERE strftime('%Y-%m', creation_date) = ?;"
                        cur = conn.cursor()
                        cur.execute(sql, (current_month_str,))
                        rows = cur.fetchall()
                        conn.close()
                        party_names_from_db = [row[0] for row in rows]
                        logger.info(
                            f"Found {len(party_names_from_db)} parties for month {current_month_str}"
                        )
                    except sqlite3.Error as e:
                        logger.error(f"Failed to fetch worksheets for month: {e}")

                if not party_names_from_db:
                    logger.info("No parties found in DB for current month.")

                for party_name in party_names_from_db:
                    try:
                        worksheets_to_check.append(spreadsheet.worksheet(party_name))
                    except WorksheetNotFound:
                        continue

            else:
                report_title = f"дохід по партії *{u_target}*"
                try:
                    ws = spreadsheet.worksheet(u_target)
                    if ws.title != PRICES_WORKSHEET_NAME:
                        worksheets_to_check.append(ws)
                except WorksheetNotFound:
                    return None, None, f"❌ Помилка: Партію `{u_target}` не знайдено."
                except Exception as e:
                    return None, None, f"❌ Помилка Google Sheets: {e}"

            for ws in worksheets_to_check:
                try:
                    user_cell = ws.find(u_name, in_column=1)
                    if not user_cell:
                        continue
                    header_row = ws.row_values(1)
                    user_row_values = ws.row_values(user_cell.row)
                    party_total = 0.0
                    for i, header in enumerate(header_row):
                        if header.endswith(" - К-сть"):
                            job_name = header.removesuffix(" - К-сть").strip()
                            if job_name in prices:
                                try:
                                    quantity = to_float(user_row_values[i])
                                    party_total += quantity * prices[job_name]
                                except (IndexError, TypeError, ValueError):
                                    continue
                    if party_total > 0:
                        total_income += party_total
                        party_count += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to process sheet {ws.title} for income: {e}"
                    )
                    continue

            return total_income, party_count, report_title

        total_income, party_count, report_title = await asyncio.to_thread(
            blocking_income_check, user_name, target
        )

        if total_income is None:
            await update.message.reply_text(report_title, parse_mode=ParseMode.MARKDOWN)
            return

        logger.info(f"Calculated income for {user_name} ({target}): {total_income}")

        if party_count == 0:
            if target == "month":
                msg = f"💸 {user_name}, ви ще не маєте доходу в цьому місяці.\n(Враховуються лише партії, *створені* цього місяця)."
            elif target == "all":
                msg = f"💸 {user_name}, ви ще не маєте доходу."
            else:
                msg = f"💸 {user_name}, ви не маєте доходу в партії *{target}*."
        else:
            count_str = (
                f"{party_count} партій" if party_count != 1 else f"{party_count} партії"
            )
            msg = (
                f"💸 Ваш {report_title}, {user_name}:\n\n"
                f"*{total_income:.2f}* грн.\n\n"
                f"(Обраховано з {count_str})"
            )

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Failed during income calculation: {e}")
        await update.message.reply_text(f"❌ Сталася непередбачена помилка: {e}")


async def get_jobs(update: Update, context: CallbackContext) -> None:
    """
    Fetches and displays the list of jobs from the Prices sheet.
    """
    await update.message.reply_text("🔄 Завантажую прайс-лист...")

    client = get_gspread_client()
    if not client:
        await update.message.reply_text(
            "❌ Помилка: Не вдалося підключитися до Google Sheets."
        )
        return

    prices = await asyncio.to_thread(load_prices, client)

    if not prices:
        await update.message.reply_text(
            f"❌ Помилка: Не вдалося завантажити прайс-лист `{PRICES_WORKSHEET_NAME}`."
        )
        return

    message_lines = ["**Доступні роботи (для копіювання):**\n"]
    for job_name in prices.keys():
        message_lines.append(f"`{job_name}`")

    # message_lines.append(f"\n*Всього: {len(prices)} робіт.*")

    await update.message.reply_text(
        "\n".join(message_lines), parse_mode=ParseMode.MARKDOWN
    )


async def get_my_work(update: Update, context: CallbackContext) -> None:
    """Fetches the user's 10 last submissions, optionally filtered by party."""
    telegram_id = update.message.from_user.id
    user_name = get_user_name(telegram_id)
    if not user_name:
        await update.message.reply_text("❌ Ви не зареєстровані. /setuser {Ваше Ім'я}")
        return

    # Check for optional party name argument
    target_party_name = " ".join(context.args).strip()
    params = [telegram_id]

    if target_party_name:
        sql = """
        SELECT timestamp, party_name, work_items_json
        FROM submissions
        WHERE telegram_id = ? AND party_name = ?
        ORDER BY timestamp DESC
        LIMIT 10;
        """
        params.append(target_party_name)
        title = f"**Ваші останні 10 записів для партії `{target_party_name}`:**\n"
    else:
        sql = """
        SELECT timestamp, party_name, work_items_json
        FROM submissions
        WHERE telegram_id = ?
        ORDER BY timestamp DESC
        LIMIT 10;
        """
        title = f"**Ваші останні 10 записів, {user_name}:**\n"

    conn = db_connect()
    submissions = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            submissions = cur.fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Error fetching user work: {e}")
            await update.message.reply_text("❌ Помилка БД при пошуку ваших записів.")
            return

    if not submissions:
        if target_party_name:
            await update.message.reply_text(
                f"❌ Не знайдено записів для партії `{target_party_name}`."
            )
        else:
            await update.message.reply_text("❌ Ви ще не зробили жодного запису.")
        return

    message_lines = [title]
    for row in submissions:
        timestamp, party_name, work_items_json = row
        try:
            dt = datetime.fromisoformat(timestamp)
            date_str = dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            date_str = timestamp.split(" ")[0]

        work_items = json.loads(work_items_json)
        items_str_list = [f"{item['action']} - {item['amount']}" for item in work_items]
        items_str = ", ".join(items_str_list)

        message_lines.append(f"*{date_str}* - `{party_name}`:\n" f"   `{items_str}`")

    await update.message.reply_text(
        "\n".join(message_lines), parse_mode=ParseMode.MARKDOWN
    )


async def rollback(update: Update, context: CallbackContext) -> None:
    """
    Asks the user to confirm rollback of their last submission.
    """
    telegram_id = update.message.from_user.id
    user_name = get_user_name(telegram_id)
    if not user_name:
        await update.message.reply_text("❌ Ви не зареєстровані. /setuser {Ваше Ім'я}")
        return

    conn = db_connect()
    last_submission = None
    if conn:
        try:
            sql = """
            SELECT submission_id, party_name, work_items_json
            FROM submissions
            WHERE telegram_id = ?
            ORDER BY timestamp DESC
            LIMIT 1;
            """
            cur = conn.cursor()
            cur.execute(sql, (telegram_id,))
            last_submission = cur.fetchone()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Error fetching last submission: {e}")
            await update.message.reply_text("❌ Помилка БД при пошуку запису.")
            return

    if not last_submission:
        await update.message.reply_text("❌ Не знайдено записів для скасування.")
        return

    submission_id, party_name, work_items_json = last_submission
    work_items = json.loads(work_items_json)

    items_str_list = [f"{item['action']} - {item['amount']}" for item in work_items]
    items_str = ", ".join(items_str_list)
    message_text = (
        f"Ви впевнені, що хочете скасувати цей запис?\n\n"
        f"*Партія:* `{party_name}`\n"
        f"*Роботи:* `{items_str}`\n\n"
        f"_(ID запису: {submission_id})_"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Так, скасувати", callback_data=f"rollback_yes_{submission_id}"
            ),
            InlineKeyboardButton("❌ Ні", callback_data="rollback_no"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
    )


async def handle_callback_query(update: Update, context: CallbackContext) -> None:
    """
    Handles inline keyboard button presses.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    telegram_id = query.from_user.id

    if data == "rollback_no":
        await query.edit_message_text("Скасування скасовано.")
        return

    elif data.startswith("rollback_yes_"):
        submission_id = int(data.split("_")[2])
        user_name = get_user_name(telegram_id)

        if not user_name:
            await query.edit_message_text("❌ Помилка: Ваш користувач не знайдений.")
            return

        conn = db_connect()
        submission_data = None
        if conn:
            try:
                sql = """
                SELECT party_name, work_items_json
                FROM submissions
                WHERE submission_id = ? AND telegram_id = ?;
                """
                cur = conn.cursor()
                cur.execute(sql, (submission_id, telegram_id))
                submission_data = cur.fetchone()
            except sqlite3.Error as e:
                logger.error(
                    f"Failed to fetch submission {submission_id} for rollback: {e}"
                )
                await query.edit_message_text("❌ Помилка БД при скасуванні.")
                conn.close()
                return

        if not submission_data:
            await query.edit_message_text(
                "❌ Помилка: Запис для скасування не знайдено (можливо, він вже скасований)."
            )
            conn.close()
            return

        party_name, work_items_json = submission_data
        work_items = json.loads(work_items_json)

        inverted_work_items = [
            {"action": item["action"], "amount": -item["amount"]} for item in work_items
        ]

        rollback_data = {
            "user_name": user_name,
            "party_name": party_name,
            "work_items": inverted_work_items,
        }

        await query.edit_message_text(f"🔄 Скасовую запис {submission_id}...")

        result_message = await asyncio.to_thread(
            process_sheets_update, data=rollback_data
        )

        if "✅" in result_message:
            try:
                sql_delete = "DELETE FROM submissions WHERE submission_id = ?;"
                cur = conn.cursor()
                cur.execute(sql_delete, (submission_id,))
                conn.commit()
                logger.info(
                    f"Successfully rolled back and deleted submission {submission_id}"
                )
                await query.edit_message_text(
                    f"✅ Запис {submission_id} успішно скасовано.\n\n"
                    f"{result_message}"
                )
            except sqlite3.Error as e:
                logger.error(
                    f"Failed to delete submission {submission_id} after rollback: {e}"
                )
                await query.edit_message_text(
                    f"✅ Запис скасовано в Google Sheets, але сталася помилка при видаленні історії: {e}"
                )
        else:
            await query.edit_message_text(
                f"❌ Не вдалося скасувати запис {submission_id}:\n\n{result_message}"
            )

        conn.close()
        return

    if data == "create_ws_no" and RESTRICTED_SHEET_CREATION:
        context.user_data.pop("pending_submission", None)
        await query.edit_message_text("❌ Скасовано. Новий аркуш не створено.")
        return
    elif data == "create_ws_yes":
        parsed_data = context.user_data.pop("pending_submission", None)

        if not parsed_data:
            await query.edit_message_text(
                "❌ Помилка: Не знайдено збережені дані.\n"
                "Будь ласка, надішліть повідомлення з даними ще раз."
            )
            return

        await query.edit_message_text(
            f"🔄 Створюю аркуш `{parsed_data['party_name']}` та додаю дані..."
        )

        success = await save_submission_to_db(parsed_data, telegram_id, context)

        if success:
            result_message = await asyncio.to_thread(
                process_sheets_update, data=parsed_data
            )
            if result_message:
                await query.message.reply_text(
                    result_message, parse_mode=ParseMode.MARKDOWN
                )
                await query.delete_message()
            else:
                await query.edit_message_text(
                    "❌ Сталася помилка під час оновлення Google Sheets."
                )
        else:
            await query.edit_message_text(
                "❌ Сталася помилка під час збереження до локальної БД."
            )
        return


def calculate_income_for_period(user_name: str, month_str: str) -> float:
    """
    Blocking function to calculate total income for a user for a specific month (YYYY-MM).
    """
    logger.info(f"Calculating report for {user_name} for month {month_str}...")
    client = get_gspread_client()
    if not client:
        logger.error("Report: Failed to connect to GSpread.")
        return 0.0
    prices = load_prices(client)
    if not prices:
        logger.error("Report: Failed to load prices.")
        return 0.0
    spreadsheet = client.open(GOOGLE_SHEET_NAME)
    total_income = 0.0
    conn = db_connect()
    party_names_from_db = []
    if conn:
        try:
            sql = "SELECT party_name FROM worksheets WHERE strftime('%Y-%m', creation_date) = ?;"
            cur = conn.cursor()
            cur.execute(sql, (month_str,))
            rows = cur.fetchall()
            conn.close()
            party_names_from_db = [row[0] for row in rows]
        except sqlite3.Error as e:
            logger.error(
                f"Report: Failed to fetch worksheets for month {month_str}: {e}"
            )
            return 0.0
    if not party_names_from_db:
        logger.info(f"Report: No parties found for {user_name} in {month_str}.")
        return 0.0
    worksheets_to_check = []
    for party_name in party_names_from_db:
        try:
            worksheets_to_check.append(spreadsheet.worksheet(party_name))
        except WorksheetNotFound:
            continue
    for ws in worksheets_to_check:
        try:
            user_cell = ws.find(user_name, in_column=1)
            if not user_cell:
                continue
            header_row = ws.row_values(1)
            user_row_values = ws.row_values(user_cell.row)
            party_total = 0.0
            for i, header in enumerate(header_row):
                if header.endswith(" - К-сть"):
                    job_name = header.removesuffix(" - К-сть").strip()
                    if job_name in prices:
                        try:
                            quantity = to_float(user_row_values[i])
                            party_total += quantity * prices[job_name]
                        except (IndexError, TypeError, ValueError):
                            continue
            total_income += party_total
        except Exception:
            continue
    logger.info(f"Report for {user_name} for {month_str}: {total_income:.2f}")
    return total_income


async def send_monthly_report(context: CallbackContext) -> None:
    """Sends a report of the previous month's income to all users."""
    logger.info("--- Running Monthly Report Job ---")
    today = date.today()
    first_day_of_current_month = today.replace(day=1)
    last_day_of_previous_month = first_day_of_current_month - timedelta(days=1)
    previous_month_str = last_day_of_previous_month.strftime("%Y-%m")
    logger.info(f"Generating report for month: {previous_month_str}")
    all_users = get_all_users()
    if not all_users:
        logger.info("Report: No users found in DB. Skipping.")
        return
    for telegram_id, user_name in all_users:
        try:
            total_income = await asyncio.to_thread(
                calculate_income_for_period, user_name, previous_month_str
            )
            if total_income > 0:
                month_name_ua = (
                    "Січень",
                    "Лютий",
                    "Березень",
                    "Квітень",
                    "Травень",
                    "Червень",
                    "Липень",
                    "Серпень",
                    "Вересень",
                    "Жовтень",
                    "Листопад",
                    "Грудень",
                )[last_day_of_previous_month.month - 1]
                message = (
                    f"💸 **Ваш звіт про дохід за {month_name_ua} {last_day_of_previous_month.year}**\n\n"
                    f"Привіт, {user_name}! Ваш загальний дохід за минулий місяць:\n\n"
                    f"*{total_income:.2f} грн.*"
                )
                await context.bot.send_message(
                    chat_id=telegram_id, text=message, parse_mode=ParseMode.MARKDOWN
                )
                logger.info(f"Sent report to {user_name} ({telegram_id})")
            else:
                logger.info(f"Skipping report for {user_name} (zero income).")
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Failed to send report to {telegram_id} ({user_name}): {e}")
    logger.info("--- Monthly Report Job Finished ---")


def main() -> None:
    """Start the bot using the new Application builder."""
    if not os.path.exists(GOOGLE_CREDS_FILE):
        logger.error(
            f"'{GOOGLE_CREDS_FILE}' not found. Please follow setup instructions."
        )
        return
    setup_database()

    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
    except InvalidToken:
        logger.error("Invalid TELEGRAM_TOKEN. Please check your token.")
        return
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        return

    # Add handlers
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setuser", set_user))
    application.add_handler(CommandHandler("myincome", get_my_income))
    application.add_handler(CommandHandler("rollback", rollback))
    application.add_handler(CommandHandler("getjobs", get_jobs))
    application.add_handler(CommandHandler("mywork", get_my_work))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    job_queue = application.job_queue
    job_queue.run_monthly(
        send_monthly_report,
        when=time(hour=9, minute=0),
        day=1,
    )
    logger.info("Scheduled monthly report job (1st of month at 9:00).")

    # Run the bot
    logger.info("Bot started...")
    try:
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot stopped with error: {e}")


if __name__ == "__main__":
    main()
