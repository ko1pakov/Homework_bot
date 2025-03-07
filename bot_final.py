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

# Установите pymorphy2: pip install pymorphy2
import pymorphy2

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def check_env_vars() -> None:
    if not GEMINI_API_KEY:
        raise ValueError("Не установлена переменная окружения GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Не установлена переменная окружения TELEGRAM_BOT_TOKEN")

check_env_vars()

# Инициализация LLM (Gemini)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# Хранилище: { "DD.MM.YYYY": [ {"subject": str, "task": str, "date": str}, ... ] }
homework_storage = {}

async def ask_model_for_json(prompt: str) -> dict | None:
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

#
# Функция «дочистки» (cleanup): убираем заведомо неверные subject ("задание" и т.п.),
# и приводим название предмета к именительному падежу через pymorphy2.
#
def cleanup_subject_and_date(text: str, subject: str, date_str: str) -> tuple[str, str]:
    # 1) Убираем ошибки
    low_subj = subject.lower().strip()
    if low_subj in ["задание", "задали", "что", "домашнее", "дз"]:
        subject = ""

    # 2) Приводим слова предмета к нормальной форме (если subject не пуст)
    if subject:
        morph = pymorphy2.MorphAnalyzer()
        # Допустим, предмет может состоять из нескольких слов: "Высшая математика"
        subj_parts = subject.split()
        normalized_parts = []
        for w in subj_parts:
            parse = morph.parse(w)[0]
            # Лемма (именительный падеж, ед. число) для прилагательных и существительных
            normal_form = parse.normal_form  # "математика", "высший", "английский" и т.д.
            normalized_parts.append(normal_form)
        # Склеиваем обратно и ставим заглавную букву
        subject = " ".join(normalized_parts).capitalize()

    # Убираем лишние пробелы у date_str
    date_str = date_str.strip()
    return subject, date_str

#
# Определение интента: add / get
#
async def parse_query(text: str) -> str:
    prompt = f"""
Определи, относится ли текст к добавлению задания ("add") или запросу заданий ("get").
Ответ дай строго в JSON виде: {{"intent": ""}}

Текст: {text}
"""
    result = await ask_model_for_json(prompt)
    if not result or "intent" not in result:
        return "unknown"
    return result["intent"]

#
# Парсинг "добавить задание"
#
async def parse_homework(text: str) -> dict | None:
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки три поля:
1. subject (название предмета). Если не упомянут, ставь "".
   Не путай слова "задание", "задали" и т.п. с названием предмета.
2. task (что конкретно задали).
3. date (DD.MM.YYYY):
   - Если "завтра"/"послезавтра", вычисли относительно {current_date}.
   - Если упомянут день недели, попытайся вычислить ближайшую дату.
   Иначе ставь "".

Ответ строго в JSON:
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

    # "Дочистка": убрать "задание" и т.п. + нормализовать падеж
    subject, date_str = cleanup_subject_and_date(text, subject, date_str)

    if not (subject or task or date_str):
        return None

    return {
        "subject": subject,
        "task": task,
        "date": date_str
    }

#
# Парсинг "запрос заданий"
#
async def parse_homework_request(text: str) -> dict | None:
    current_date = datetime.now().strftime("%A, %d.%m.%Y")

    prompt = f"""
Проанализируй текст и извлеки:
1. subject (название предмета) - если не упомянут, ставь "".
   Не путай слова "задание", "задали" и т.п. с названием предмета.
2. date (дата в формате DD.MM.YYYY) - если не упомянута, ставь "".
   Если есть "завтра"/"послезавтра"/день недели, вычисли относительно {current_date}.

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

    # "Дочистка": убрать "задание" и т.п. + нормализовать падеж
    subject, date_str = cleanup_subject_and_date(text, subject, date_str)

    return {
        "subject": subject,
        "date": date_str
    }

#
# Логика выборки
#
def get_tasks_by_filter(subject: str | None, date_str: str | None) -> list[str]:
    """
    Правила:
      - Если есть subject, но нет date -> показываем задания по этому предмету, начиная с завтра.
      - Если есть date, но нет subject -> все задания на указанную дату.
      - Если есть и subject, и date -> задания для этого предмета и даты.
      - Если нет ни subject, ни date -> все задания.
    """
    results = []

    if not subject:
        subject = None
    if not date_str:
        date_str = None

    # 1) есть subject, нет date
    if subject and not date_str:
        tomorrow = datetime.now() + timedelta(days=1)
        for d_str, hw_list in homework_storage.items():
            try:
                dt = datetime.strptime(d_str, "%d.%m.%Y")
            except ValueError:
                continue
            if dt >= tomorrow:
                for hw in hw_list:
                    if hw["subject"].lower() == subject.lower():
                        results.append(f"Дата: {d_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # 2) есть date, нет subject
    if date_str and not subject:
        hw_list = homework_storage.get(date_str, [])
        for hw in hw_list:
            results.append(f"Дата: {date_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # 3) есть и subject, и date
    if subject and date_str:
        hw_list = homework_storage.get(date_str, [])
        for hw in hw_list:
            if hw["subject"].lower() == subject.lower():
                results.append(f"Дата: {date_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
        return results

    # 4) нет ни subject, ни date
    for d_str, hw_list in homework_storage.items():
        for hw in hw_list:
            results.append(f"Дата: {d_str}\nПредмет: {hw['subject']}\nЗадание: {hw['task']}")
    return results

#
# Telegram-хендлеры
#

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для управления заданиями.\n\n"
        "Пример: «По математике на завтра задания 431, 432».\n"
        "Или спросите: «Что задали на понедельник?»."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_input = update.message.text
    intent = await parse_query(user_input)

    if intent == "add":
        hw = await parse_homework(user_input)
        if not hw:
            await update.message.reply_text("❌ Не удалось распознать задание.")
            return

        date = hw["date"]
        subject = hw["subject"]
        task = hw["task"]

        # Сохраняем
        homework_storage.setdefault(date, []).append(hw)

        await update.message.reply_text(
            f"✅ Задание добавлено:\n"
            f"Предмет: {subject or '(не указан)'}\n"
            f"Дата: {date or '(не указана)'}\n"
            f"Задание: {task}"
        )
    elif intent == "get":
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
            # Формируем текст ошибки
            if subject and date_str:
                await update.message.reply_text(f"❌ Заданий по предмету '{subject}' на {date_str} не найдено.")
            elif subject:
                await update.message.reply_text(f"❌ Заданий по предмету '{subject}' не найдено (завтра и далее).")
            elif date_str:
                await update.message.reply_text(f"❌ Заданий на {date_str} не найдено.")
            else:
                await update.message.reply_text("❌ Нет заданий.")
    else:
        await update.message.reply_text("❌ Не понял, нужно добавить задание или посмотреть имеющиеся?")

#
# Запуск бота
#
if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()
