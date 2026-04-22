import os, json, logging, tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, CommandHandler
)
import anthropic, gspread, openai
from google.oauth2.service_account import Credentials
from datetime import datetime

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
import json as _json, tempfile as _tmp
_f = _tmp.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
_f.write(os.getenv("GOOGLE_CREDS_JSON", "{}"))
_f.flush()
GOOGLE_CREDS = _f.name

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
oai    = openai.OpenAI(api_key=OPENAI_KEY)

STATUSES     = ["В роботі", "На холді", "Готово", "Не в роботі"]
STATUS_ICONS = {"В роботі": "⚡", "На холді": "⏸", "Готово": "✅", "Не в роботі": "❌"}

REVIEWING, PICKING_PROJECT, TYPING_PROJECT, PICKING_PRIORITY, PICKING_STATUS = range(5)

def sheets():
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def get_projects():
    try:
        vals = sheets().worksheet("Довідники").col_values(1)
        return [v.strip() for v in vals[1:] if v.strip()]
    except:
        return []

def add_project_to_sheet(name):
    sheets().worksheet("Довідники").append_row([name])

def save_task(task):
    sheets().worksheet("Задачі").append_row([
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        task.get("name", ""),
        task.get("project", ""),
        task.get("description", ""),
        task.get("priority", "3"),
        task.get("deadline", ""),
        task.get("status", "В роботі"),
        task.get("comments", ""),
    ])

def extract_task(text, projects):
    plist = ", ".join(projects) if projects else "список порожній"
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role": "user", "content": (
            "Ти - асистент для управління задачами. Витягни задачу з повідомлення.\n"
            "Доступні проекти: " + plist + "\n"
            "Повідомлення: " + text + "\n\n"
            "Поверни ТІЛЬКИ валідний JSON:\n"
            '{"name": "назва задачі до 8 слів",'
            '"project": "точна назва проекту з переліку АБО порожньо",'
            '"description": "детальний опис задачі",'
            '"priority": "число 1-5",'
            '"deadline": "ДД.ММ.РРРР або порожньо",'
            '"status": "В роботі",'
            '"comments": "додаткова інформація або порожньо"}'
        )}]
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

async def transcribe(path):
    with open(path, "rb") as f:
        return oai.audio.transcriptions.create(
            model="whisper-1", file=f, language="uk"
        ).text

def task_text(task):
    proj = task.get("project") or "не вибрано"
    prio = task.get("priority", "3")
    stat = task.get("status", "В роботі")
    icon = STATUS_ICONS.get(stat, "")
    ddl  = task.get("deadline") or "не вказано"
    comm = "\n" + task["comments"] if task.get("comments") else ""
    return (
        "Нова задача\n\n"
        + task.get("name", "-") + "\n"
        + "Проект: " + proj + "\n"
        + task.get("description", "-") + "\n"
        + "Пріоритет: " + prio + "/5\n"
        + "Дедлайн: " + ddl + "\n"
        + icon + " Статус: " + stat + comm + "\n\n"
        + "Перевір і натисни Зберегти"
    )

def review_kb(task):
    proj = task.get("project") or "не вибрано"
    stat = task.get("status", "В роботі")
    prio = task.get("priority", "3")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Проект: " + proj, callback_data="pick_project")],
        [InlineKeyboardButton("Пріоритет: " + prio, callback_data="pick_priority"),
         InlineKeyboardButton(stat, callback_data="pick_status")],
        [InlineKeyboardButton("Зберегти задачу", callback_data="save")],
        [InlineKeyboardButton("Скасувати", callback_data="cancel")],
    ])

def project_kb(projects):
    rows = [[InlineKeyboardButton(p, callback_data="proj:" + p)] for p in projects]
    rows.append([InlineKeyboardButton("+ Новий проект", callback_data="new_project")])
    rows.append([InlineKeyboardButton("Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

def priority_kb():
    labels = [("1","мінімальний"),("2","низький"),("3","середній"),
              ("4","високий"),("5","критичний")]
    rows = [[InlineKeyboardButton(n + " - " + l, callback_data="prio:" + n)] for n,l in labels]
    rows.append([InlineKeyboardButton("Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

def status_kb():
    rows = [[InlineKeyboardButton(s, callback_data="stat:" + s)] for s in STATUSES]
    rows.append([InlineKeyboardButton("Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def _start_task(text, update, ctx):
    await update.message.reply_text("Аналізую...")
    projects = get_projects()
    task = extract_task(text, projects)
    ctx.user_data["task"] = task
    ctx.user_data["projects"] = projects
    await update.message.reply_text(
        task_text(task), reply_markup=review_kb(task)
    )
    return REVIEWING

async def handle_text(update, ctx):
    text = update.message.text or ""
    if update.message.forward_date:
        text = "[Переслано] " + text
    return await _start_task(text, update, ctx)

async def handle_voice(update, ctx):
    await update.message.reply_text("Транскрибую голос...")
    file = await ctx.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        text = await transcribe(tmp.name)
    await update.message.reply_text("Розпізнано: " + text)
    return await _start_task(text, update, ctx)

async def callbacks(update, ctx):
    q = update.callback_query
    await q.answer()
    d = q.data
    task = ctx.user_data.get("task", {})

    if d == "save":
        save_task(task)
        stat = task.get("status", "В роботі")
        await q.edit_message_text(
            "Збережено!\n\n" + task.get("name","") + "\n"
            + task.get("project","") + "  |  "
            + task.get("priority","3") + "/5  |  " + stat
        )
        return ConversationHandler.END
    elif d == "cancel":
        await q.edit_message_text("Скасовано.")
        return ConversationHandler.END
    elif d == "pick_project":
        await q.edit_message_text(
            "Вибери проект:",
            reply_markup=project_kb(ctx.user_data.get("projects", []))
        )
        return PICKING_PROJECT
    elif d == "pick_priority":
        await q.edit_message_text("Вибери пріоритет:", reply_markup=priority_kb())
        return PICKING_PRIORITY
    elif d == "pick_status":
        await q.edit_message_text("Вибери статус:", reply_markup=status_kb())
        return PICKING_STATUS
    elif d.startswith("proj:"):
        task["project"] = d[5:]
        ctx.user_data["task"] = task
    elif d.startswith("prio:"):
        task["priority"] = d[5:]
        ctx.user_data["task"] = task
    elif d.startswith("stat:"):
        task["status"] = d[5:]
        ctx.user_data["task"] = task
    elif d == "new_project":
        await q.edit_message_text("Напиши назву нового проекту:")
        return TYPING_PROJECT

    if d != "new_project":
        await q.edit_message_text(task_text(task), reply_markup=review_kb(task))
        return REVIEWING
    return REVIEWING

async def save_new_project(update, ctx):
    name = update.message.text.strip()
    add_project_to_sheet(name)
    task = ctx.user_data.get("task", {})
    task["project"] = name
    ctx.user_data["task"] = task
    projects = ctx.user_data.get("projects", [])
    projects.append(name)
    ctx.user_data["projects"] = projects
    await update.message.reply_text(
        "Проект " + name + " додано!\n\n" + task_text(task),
        reply_markup=review_kb(task)
    )
    return REVIEWING

async def weekly_plan(update, ctx):
    await update.message.reply_text("Генерую план...")
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content":
            "Згенеруй мотиваційний план-намір на робочий тиждень. "
            "5-7 конкретних намірів, по-українськи. "
            "Стиль: енергійний, практичний. "
            "Починай одразу з першого пункту без вступу."
        }]
    )
    await update.message.reply_text(resp.content[0].text)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.VOICE, handle_voice),
        ],
        states={
            REVIEWING:        [CallbackQueryHandler(callbacks)],
            PICKING_PROJECT:  [CallbackQueryHandler(callbacks)],
            TYPING_PROJECT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, save_new_project)],
            PICKING_PRIORITY: [CallbackQueryHandler(callbacks)],
            PICKING_STATUS:   [CallbackQueryHandler(callbacks)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_message=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("plan", weekly_plan))
    print("Бот запущено!")
    app.run_polling()

if __name__ == "__main__":
    main()
