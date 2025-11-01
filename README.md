# Jmilonok Billing Bot: Інструкція з експлуатації

## 1. Вимоги
- Python 3
- Файли:
     - `jmilonok.py` (скрипт бота)
     - `requirements.txt` (список бібліотек)
     - Google service account JSON 
     - Термінал (Ubuntu або інша ОС)

### Встановлення залежностей:
```bash
pip install -r requirements.txt

# Або окремо:
pip install python-telegram-bot gspread google-auth
```

## 2. Отримання Telegram Bot Token
1. У Telegram відкрийте BotFather
2. Надішліть `/newbot` та дотримуйтесь інструкцій
3. Скопіюйте отриманий токен та встановіть його у `TELEGRAM_TOKEN` в `jmilonok.py`

## 3. Налаштування Google Sheets API
1. У Google Cloud Console створіть новий проект
2. Увімкніть API:
      - Google Drive API
      - Google Sheets API
3. Створіть Сервісний Акаунт (APIs & Services → Credentials → Create credentials → Service account)
4. Надайте акаунту роль "Editor"
5. Створіть та завантажте JSON-ключ
7. Покладіть його в папку із `jmilonok.py`

## 4. Налаштування доступу до Google Sheet
1. Відкрийте JSON та скопіюйте `client_email`
2. Створіть нову Google Таблицю
3. "Поділіться" таблицею з `client_email`, надавши права Editor
4. Створіть аркуш "Prices" з заголовками:

| Робота | Ціна |
|--------|------|
| Збірка | 10   |
| Пайка  | 25.5 |
| Тест   | 15   |

5. Створіть аркуші для партій (напр. "Партія 101", "Проект Альфа")

## 5. Налаштування та запуск
Відредагуйте `jmilonok.py`:
```python
TELEGRAM_TOKEN = "ваш_токен"
GOOGLE_SHEET_NAME = "назва_таблиці"
GOOGLE_CREDS_FILE = "назва_json"
DATABASE_FILE = "db.db"
```

Запуск:
```bash
python3 jmilonok.py
```

## 6. Використання

### 6.1 Реєстрація
```
/setuser ВашеІм'я
```

### 6.2 Внесення даних
```
Назва Партії: Робота1 - Кількість1, Робота2 - Кількість2;
```

### 6.3 Перевірка доходу
```
/myincome           # поточний місяць
/myincome all      # весь час
/myincome Партія 101  # конкретна партія
```

### 6.4 Інші команди
- `/mywork [партія]` - історія записів
- `/rollback` - скасування останнього запису
- `/getjobs` - список доступних робіт

## 7. Приклад роботи
```
/setuser Іван
Партія 102: Збірка - 5, Пайка - 2;
/myincome
```
## 8. Вирішення проблем
- "Ви не зареєстровані" → `/setuser`
- "Робота не знайдена" → перевірте Prices
- "Аркуш не знайдено" → створіть вкладку
- "Помилка підключення" → перевірте JSON

## 9. Команди
- `/start` - меню
- `/help` - довідка
- `/setuser` - реєстрація
- `/myincome` - дохід
- `/getjobs` - роботи
- `/mywork` - історія
- `/rollback` - скасування