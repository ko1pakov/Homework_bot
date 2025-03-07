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
    if not GEMINI_API_KEY:
        raise ValueError("Не установлена переменная окружения GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Не установлена переменная окружения TELEGRAM_BOT_TOKEN")

check_env_vars()

# -------------------------
# Инициализация LLM (Gemini)
# -------------------------

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# -------------------------
# Временное хранилище заданий (в памяти)
# Формат: { "DD.MM.YYYY": [ {"subject": str, "task": str, "date": str}, ... ] }
# -------------------------

homework_storage = {}

# -------------------------
# Вспомогательная функция
# -------------------------

async def ask_model_for_json(prompt: str) -> dict | None:
    """
    Шлёт prompt в модель, пытается распарсить ответ как JSON.
    Возвращает dict или None при ошибке.
    """
    try:
        response = model.generate_content(prompt)
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
# Функции «дочистки» для subject/date
# -------------------------

def cleanup_subject_and_date(text: str, subject: str, date_str: str) -> tuple[str, str]:
    """
    1. Если LLM вернула subject == 'задание', 'задали' и т.п. (то есть явно некорректный предмет),
       то обнуляем subject.
    2. (Дополнительно можно сюда же добавить парсинг дней недели, если хотите.)
    """
    # На всякий случай
    low_subj = subject.lower().strip()
    if low_subj in ["задание", "задали", "что"]:
        subject = ""

    # Можно убрать пробелы
    subject = subject.strip()
    date_str = date_str.strip()

    return subject, date_str

# -------------------------
# Парсинг запроса: add/get
# -------------------------

async def parse_query(text: str) -> str:
    """
    LLM говорит, что это "add" (добавить задание) или "get" (получить).
    """
    prompt = f"""
Определи, относится ли текст к добавлению задания ("add") или запросу заданий ("get").
Ответ дай строго в JSON виде: {{"intent": ""}}

Текст: {text}
"""
    result = await ask_model_for_json(prompt)
    if not result or "intent" not in result:
        return "unknown"
    return result["intent"]

# -------------------------
# Парсинг «добавить задание» (subject, task, date)
# -------------------------

async def parse_homework(text: str) -> dict | None:
    """
    Извлекает:
      - subject (предмет) или "" если нет
      - task (текст задания)
      - date (DD.MM.YYYY) или "" если не удалось извлечь
    """
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки три поля:
1. subject (название предмета). Если в тексте конкретный предмет не упомянут, ставь "".
   Не путай слова "задание", "задали" и т.п. с названием предмета.
2. task (само задание: что нужно сделать).
3. date (дата в формате DD.MM.YYYY), если упоминается:
   - Если видишь "завтра"/"послезавтра", вычисли относительно {current_date}.
   - Если видишь конкретный день недели ("понедельник" и т.д.), вычисли ближайший (но модель может ошибаться).
   Если дата не упоминается, ставь "".

Ответ **строго** в JSON формате:
{{
  "subject": "",
  "task": "",
  "date": ""
}}

Текст: {text}
"""
    result = await ask_model_for_json(prompt)
    if not result:
        return None

    subject = result.get("subject", "").strip()
    task = result.get("task", "").strip()
    date_str = result.get("date", "").strip()

    # "Дочистка"
    subject, date_str = cleanup_subject_and_date(text, subject, date_str)

    # Если совсем ничего не получилось
    if not (subject or task or date_str):
        return None

    return {
        "subject": subject.capitalize() if subject else "",
        "task": task,
        "date": date_str
    }

# -------------------------
# Парсинг «получить задания» (subject, date)
# -------------------------

async def parse_homework_request(text: str) -> dict | None:
    """
    Извлекает subject (если есть) и date (если есть).
    Если LLM не найдёт предмет/дату, вернёт "".
    """
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки:
1. subject (название предмета) - если не упомянут в тексте, ставь "".
   Не путай слова "задание", "задали" с названием предмета.
2. date (дата в формате DD.MM.YYYY).
   - Если видишь "завтра"/"послезавтра", вычисли относительно {current_date}.
   - Если видишь дни недели ("на понедельник", "во вторник" и т.д.), попытайся вычислить ближайшую дату.
   Если не упоминается дата, ставь "".

Ответ строго в JSON:
{{
  "subject": "",
  "date": ""
}}

Текст: {text}
"""

    result = await ask_model_for_json(prompt)
    if not result:
        return None

    subject = result.get("subject", "").strip()
    date_str = result.get("date", "").strip()

    # "Дочистка"
    subject, date_str = cleanup_subject_and_date(text, subject, date_str)

    return {
        "subject": subject.capitalize() if subject else "",
        "date": date_str
    }

# -------------------------
# Логика выборки заданий с учётом «нет даты» / «нет предмета»
# -------------------------

def get_tasks_by_filter(subject: str | None, date_str: str | None) -> list[str]:
    """
    Возвращает список заданий в текстовом виде с учётом логики:
      - если есть subject, но нет date -> вывести все задания по этому предмету, начиная с завтра
      - если есть date, но нет subject -> вывести все задания на эту дату
      - если есть и subject, и date -> вывести задания по subject на date
      - иначе (нет ничего) -> вывести все задания (или ничего, зависит от желаемого поведения)
    """

    results = []

    # Превратим пустые строки в None
    if subject == "":
        subject = None
    if date_str == "":
        date_str = None

    # вариант: subject + нет date
    if subject and not date_str:
        tomorrow = datetime.now() + timedelta(days=1)
        for d_str, hw_list in homework_storage.items():
            try:
                dt = datetime.strptime(d_str, "%d.%m.%Y")
            except ValueError:
                continue
            if dt >= tomorrow:
                # ищем задания по нужному предмету
                for hw in hw_list:
                    if hw["subject"].lower() == subject.lower():
                        results.append(f"Дата: {d_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # вариант: date есть, subject нет
    if date_str and not subject:
        hw_list = homework_storage.get(date_str, [])
        for hw in hw_list:
            results.append(f"Дата: {date_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # вариант: есть и subject, и date
    if subject and date_str:
        hw_list = homework_storage.get(date_str, [])
        for hw in hw_list:
            if hw["subject"].lower() == subject.lower():
                results.append(f"Дата: {date_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # вариант: нет ни subject, ни date -> покажем все (или вы можете вернуть [] если хотите)
    for d_str, hw_list in homework_storage.items():
        for hw in hw_list:
            results.append(f"Дата: {d_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")

    return results

# -------------------------
# Хендлеры Telegram
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для управления заданиями.\n\n"
        "Напиши, что задали, например:\n"
        "«По математике на завтра упражнения 431, 432»\n"
        "Или спроси: «Что задали на понедельник?»"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_input = update.message.text
    intent = await parse_query(user_input)

    if intent == "add":
        # Парсим задание (subject, task, date)
        hw = await parse_homework(user_input)
        if not hw:
            await update.message.reply_text("❌ Не удалось распознать задание.")
            return
        date = hw["date"]
        subject = hw["subject"]
        task = hw["task"]

        # Добавляем в наше "homework_storage"
        homework_storage.setdefault(date, []).append(hw)

        await update.message.reply_text(
            f"✅ Задание добавлено:\n"
            f"Предмет: {subject or '(не указан)'}\n"
            f"Дата: {date or '(не указана)'}\n"
            f"Задание: {task}"
        )

    elif intent == "get":
        # Парсим запрос (subject, date)
        request_data = await parse_homework_request(user_input)
        if not request_data:
            await update.message.reply_text("❌ Не удалось распознать запрос.")
            return

        subject = request_data["subject"]
        date_str = request_data["date"]

        tasks = get_tasks_by_filter(subject, date_str)
        if tasks:
            await update.message.reply_text("\n\n".join(tasks))
        else:
            # Выводим сообщение о том, что не нашли
            if subject and date_str:
                await update.message.reply_text(
                    f"❌ Заданий по предмету '{subject}' на {date_str} не найдено."
                )
            elif subject:
                await update.message.reply_text(
                    f"❌ Заданий по предмету '{subject}' не найдено (завтра и далее)."
                )
            elif date_str:
                await update.message.reply_text(
                    f"❌ Заданий на {date_str} не найдено."
                )
            else:
                await update.message.reply_text("❌ Нет заданий.")
    else:
        # Не удалось определить intent
        await update.message.reply_text("❌ Я не понял, что вы хотите: добавить задание или посмотреть?")

# -------------------------
# Запуск бота
# -------------------------
if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()
