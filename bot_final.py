import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
import google.generativeai as genai
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# -------------------------
# Загрузка и проверка окружения
# -------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def check_env_vars() -> None:
    """Проверяем, что все нужные переменные окружения установлены."""
    if not GEMINI_API_KEY:
        raise ValueError("Не установлена переменная окружения GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Не установлена переменная окружения TELEGRAM_BOT_TOKEN")

check_env_vars()

# -------------------------
# Инициализация LLM (Gemini)
# -------------------------

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash-001")

# -------------------------
# Временное хранилище заданий (в памяти)
# Формат: { "DD.MM.YYYY": [ {"subject": str, "task": str, "date": str}, ... ] }
# -------------------------

homework_storage = {}

# -------------------------
# Общая функция для запроса к LLM и парсинга ответа как JSON
# -------------------------

async def ask_model_for_json(prompt: str) -> dict | None:
    """
    Отправляет prompt в модель и пытается распарсить ответ как JSON.
    Возвращает dict при успехе или None при ошибке.
    """
    try:
        response = model.generate_content(prompt)
        # Удаляем возможные обёртки ```json ... ```
        json_str = (
            response.text
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )
        return json.loads(json_str)
    except Exception as e:
        print(f"[ask_model_for_json] Ошибка парсинга ответа: {e}")
        return None

# -------------------------
# Парсеры
# -------------------------

async def parse_query(text: str) -> str:
    """
    Определяет тип запроса:
    - "add" (добавление задания)
    - "get" (получение заданий)
    - "unknown" (если не удалось определить)
    """
    prompt = f"""
Определи тип запроса: "add" (добавление задания) или "get" (получение заданий).
Ответ дай только в JSON:
{{"intent": ""}}

Текст: {text}
"""
    result = await ask_model_for_json(prompt)
    if not result or "intent" not in result:
        return "unknown"
    return result["intent"]

async def parse_homework(text: str) -> dict | None:
    """
    Извлекает из текста:
    - subject (предмет)
    - task (задание)
    - date (дата в формате DD.MM.YYYY)
      Если дата указана словами (например, 'завтра'), она должна быть вычислена
      относительно текущей даты.
    Возвращает dict {"subject": str, "task": str, "date": str} или None при ошибке.
    """
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки:
1. Предмет (subject)
2. Задание (task)
3. Дату в формате DD.MM.YYYY (date)

Если дата указана словами (например, 'завтра'), вычисли актуальную дату. Текущая дата: {current_date}
Ответ должен быть только в формате JSON:
{{"subject": "", "task": "", "date": ""}}

Текст: {text}
"""

    result = await ask_model_for_json(prompt)
    if not result:
        return None

    # Приводим поля к нужному виду (если их нет, считаем что None)
    subject = result.get("subject") or ""
    task = result.get("task") or ""
    date = result.get("date") or ""

    # Минимальная проверка
    if not subject and not task and not date:
        return None

    return {
        "subject": subject.capitalize(),
        "task": task,
        "date": date
    }

async def parse_homework_request(text: str) -> dict | None:
    """
    Извлекает из текста:
    - subject (предмет)
    - date (дата в формате DD.MM.YYYY)
      (Если указан «завтра» или др. слова — LLM должна вернуть конечную дату)
    Возвращает dict {"subject": str, "date": str} или None при ошибке.
    """
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки:
1. Предмет (subject)
2. Дату в формате DD.MM.YYYY (date)

Если дата указана словами (например, 'завтра'), вычисли актуальную дату. Текущая дата: {current_date}
Ответ должен быть только в формате JSON:
{{"subject": "", "date": ""}}

Текст: {text}
"""

    result = await ask_model_for_json(prompt)
    if not result:
        return None

    # Приводим поля к виду (None, если пусто)
    subject = result.get("subject") or ""
    date = result.get("date") or ""

    return {
        "subject": subject.capitalize() if subject else "",
        "date": date
    }

# -------------------------
# Функция для выборки заданий из homework_storage
# -------------------------

def get_tasks_by_filter(subject: str | None, date: str | None) -> list[str]:
    """
    Возвращает список задач в текстовом формате по указанным фильтрам subject и date.
    Если subject или date = None/пустая строка, значит фильтра по нему нет.
    """
    results = []

    subject_filter = subject if subject else None
    date_filter = date if date else None

    if date_filter is not None:
        # Ищем только в указанной дате
        homeworks = homework_storage.get(date_filter, [])
        for hw in homeworks:
            if subject_filter is None or hw["subject"] == subject_filter:
                results.append(
                    f"Дата: {date_filter}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}"
                )
    else:
        # По всем датам
        for d, homeworks in homework_storage.items():
            for hw in homeworks:
                if subject_filter is None or hw["subject"] == subject_filter:
                    results.append(
                        f"Дата: {d}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}"
                    )

    return results

# -------------------------
# Хендлеры команд и сообщений
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет приветственное сообщение при /start
    """
    await update.message.reply_text(
        "Привет! Я бот для управления заданиями.\n\n"
        "Примеры команд:\n"
        "- Добавить задание: 'По математике на завтра задали номера 431, 432'\n"
        "- Посмотреть задания: 'Что задали на завтра?'"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработка текстовых сообщений (не команд).
    1. Узнаём интент (add/get).
    2. Если add — парсим задание и добавляем в storage.
    3. Если get — парсим запрос и выводим задания по фильтрам.
    """
    user_input = update.message.text
    intent = await parse_query(user_input)

    if intent == "add":
        homework = await parse_homework(user_input)
        if homework is not None:
            date = homework["date"]
            # Добавляем задание в хранилище
            homework_storage.setdefault(date, []).append(homework)
            await update.message.reply_text(
                f"✅ Задание добавлено:\n"
                f"Предмет: {homework['subject']}\n"
                f"Дата: {date}\n"
                f"Задание: {homework['task']}"
            )
        else:
            await update.message.reply_text("❌ Не удалось распознать задание. Попробуйте другой формат.")

    elif intent == "get":
        request_data = await parse_homework_request(user_input)
        if request_data is None:
            await update.message.reply_text("❌ Не удалось распознать запрос. Попробуйте другой формат.")
            return

        subject = request_data["subject"]
        date = request_data["date"]
        # Пустая строка в subject/date будет означать «отсутствие» фильтра
        subject = subject if subject else None
        date = date if date else None

        tasks = get_tasks_by_filter(subject, date)
        if tasks:
            await update.message.reply_text("\n\n".join(tasks))
        else:
            # Формируем текст ошибки
            if subject and date:
                await update.message.reply_text(f"❌ Заданий по предмету '{subject}' на {date} не найдено.")
            elif subject:
                await update.message.reply_text(f"❌ Заданий по предмету '{subject}' не найдено.")
            elif date:
                await update.message.reply_text(f"❌ Заданий на {date} не найдено.")
            else:
                await update.message.reply_text("❌ Не удалось найти подходящих заданий.")
    else:
        # Интент "unknown"
        await update.message.reply_text("❌ Не удалось определить тип запроса. Попробуйте другой формат.")

# -------------------------
# Запуск бота
# -------------------------

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()
