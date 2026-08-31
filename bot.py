"""
Телеграм-бот для учёта тренировок.

Логика:
- Тренировки идут по циклу День 1 -> День 2 -> День 3 -> День 1 -> ...
  Бот сам предлагает следующий день, но его можно сменить кнопкой.
- После выбора дня показывается МЕНЮ упражнений этого дня — можно выбрать
  любое в любом порядке, пропустить, вернуться назад к списку в любой момент
  (кнопка "↩️ К списку упражнений") или пропустить текущий подход
  ("⏭ Пропустить подход"). Готовые упражнения отмечаются ✅.
- Внутри упражнения вес и повторения вводятся через кнопки-цифры (без
  свободного текста) для подхода 1, 2, 3.
- /reset — удаляет все сохранённые тренировки текущего пользователя (для
  очистки тестовых данных).
- Все данные сохраняются в SQLite (файл, путь берётся из переменной окружения DB_PATH).
- Отдельный HTTP-эндпоинт /export отдаёт все записи в JSON по секретному токену —
  через него внешний скрипт (запускаемый Клодом в чате) синхронизирует данные
  в xlsx-файл на компьютере пользователя.

Настройки берутся из переменных окружения:
  BOT_TOKEN     - токен бота от @BotFather (обязательно)
  EXPORT_TOKEN  - произвольный секретный токен для /export (обязательно)
  DB_PATH       - путь к файлу базы данных (по умолчанию ./workouts.db)
  PORT          - порт для HTTP-сервера (Railway подставляет сам)
"""

import asyncio
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from contextlib import closing

from fastapi import FastAPI, HTTPException, Query
import uvicorn

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
EXPORT_TOKEN = os.environ["EXPORT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "workouts.db")
PORT = int(os.environ.get("PORT", "8080"))
MSK = timezone(timedelta(hours=3))

# Программа тренировок — как в исходном шаблоне "Дневник тренировок.xlsx".
# Порядок дней и упражнений внутри дня важен: именно в этом порядке они
# попадут в таблицу.
PROGRAM = {
    1: [
        ("Ноги", "Приседания со штангой"),
        ("Грудь", "Жим штанги лежа"),
        ("Спина", "Подтягивания"),
        ("Пресс", "Подъем ног в висе"),
    ],
    2: [
        ("Ноги", "Румынская тяга"),
        ("Грудь", "Отжимания на брусьях"),
        ("Спина", "Тяга верхнего блока к груди"),
        ("Плечи", "Армейский жим"),
    ],
    3: [
        ("Ноги", "Жим ногами в тренажере"),
        ("Грудь", "Жим гантелей лежа на наклонной скамье"),
        ("Спина", "Тяга горизонтального блока к поясу"),
        ("Икры", "Подъем носков сидя"),
    ],
}
SETS_PER_EXERCISE = 3


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            day_number INTEGER NOT NULL,
            muscle_group TEXT NOT NULL,
            exercise TEXT NOT NULL,
            set_number INTEGER NOT NULL,
            weight REAL,
            reps INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def last_day_number(chat_id: int) -> int | None:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT day_number FROM sets WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row[0] if row else None


def save_set(chat_id, session_date, day_number, muscle_group, exercise, set_number, weight, reps):
    with closing(db()) as conn:
        conn.execute(
            """INSERT INTO sets
               (chat_id, session_date, day_number, muscle_group, exercise, set_number,
                weight, reps, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                chat_id, session_date, day_number, muscle_group, exercise, set_number,
                weight, reps, datetime.now(MSK).isoformat(),
            ),
        )
        conn.commit()


def all_rows():
    with closing(db()) as conn:
        cur = conn.execute(
            """SELECT chat_id, session_date, day_number, muscle_group, exercise,
                      set_number, weight, reps, created_at
               FROM sets ORDER BY session_date, day_number, id"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def delete_all(chat_id=None):
    """Удаляет все записи (или только для конкретного chat_id)."""
    with closing(db()) as conn:
        if chat_id is None:
            conn.execute("DELETE FROM sets")
        else:
            conn.execute("DELETE FROM sets WHERE chat_id=?", (chat_id,))
        conn.commit()


def sets_done_for(chat_id, session_date, day_number, exercise):
    with closing(db()) as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM sets
               WHERE chat_id=? AND session_date=? AND day_number=? AND exercise=?""",
            (chat_id, session_date, day_number, exercise),
        ).fetchone()
        return row[0] if row else 0


# --------------------------------------------------------------------------
# Состояние диалога (в памяти, per chat_id)
# --------------------------------------------------------------------------
# state = {
#   "day": int, "session_date": "YYYY-MM-DD",
#   "ex_idx": int, "set_no": int,
#   "stage": "weight" | "reps", "buffer": str, "weight": float
# }
SESSIONS: dict[int, dict] = {}


def kb_digits(prefix: str, allow_dot: bool) -> InlineKeyboardMarkup:
    rows = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]]
    last_row = ["0", ".", "⌫"] if allow_dot else ["0", "⌫", "⌫"]
    buttons = [[InlineKeyboardButton(d, callback_data=f"{prefix}:{d}") for d in r] for r in rows]
    buttons.append([InlineKeyboardButton(d, callback_data=f"{prefix}:{d}") for d in last_row])
    buttons.append([
        InlineKeyboardButton("⏭ Пропустить подход", callback_data="nav:skipset"),
        InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}:done"),
    ])
    buttons.append([
        InlineKeyboardButton("↩️ К списку упражнений", callback_data="nav:menu"),
    ])
    return InlineKeyboardMarkup(buttons)


def day_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"День {d}", callback_data=f"startday:{d}") for d in (1, 2, 3)
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    suggested = (last_day_number(chat_id) or 0) % 3 + 1
    await update.message.reply_text(
        f"Привет! По циклу сегодня должен быть День {suggested}.\n"
        "Выбери день тренировки:",
        reply_markup=day_choice_kb(),
    )


def menu_kb(chat_id) -> InlineKeyboardMarkup:
    st = SESSIONS[chat_id]
    exercises = PROGRAM[st["day"]]
    rows = []
    for i, (group, exercise) in enumerate(exercises):
        done = sets_done_for(chat_id, st["session_date"], st["day"], exercise)
        mark = "✅ " if done >= SETS_PER_EXERCISE else (f"({done}/{SETS_PER_EXERCISE}) " if done else "")
        rows.append([InlineKeyboardButton(f"{mark}{exercise}", callback_data=f"menu:{i}")])
    rows.append([InlineKeyboardButton("🏁 Завершить тренировку", callback_data="nav:finish")])
    return InlineKeyboardMarkup(rows)


async def show_menu(query, chat_id, note: str = ""):
    st = SESSIONS[chat_id]
    text = (
        f"День {st['day']} · {st['session_date']}\n"
        f"{note}Выбери упражнение:"
    )
    await query.edit_message_text(text, reply_markup=menu_kb(chat_id))


async def on_start_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    day = int(query.data.split(":")[1])
    SESSIONS[chat_id] = {
        "day": day,
        "session_date": datetime.now(MSK).strftime("%Y-%m-%d"),
        "ex_idx": None,
        "set_no": 1,
        "stage": "menu",
        "buffer": "",
        "weight": None,
    }
    await show_menu(query, chat_id)


def current_exercise(chat_id):
    st = SESSIONS[chat_id]
    return PROGRAM[st["day"]][st["ex_idx"]]


async def ask_current(query, chat_id):
    st = SESSIONS[chat_id]
    group, exercise = current_exercise(chat_id)
    stage_label = "вес (кг)" if st["stage"] == "weight" else "повторения"
    text = (
        f"День {st['day']} · {st['session_date']}\n"
        f"Упражнение: {exercise} ({group})\n"
        f"Подход {st['set_no']} из {SETS_PER_EXERCISE}\n"
        f"Введи {stage_label}: {st['buffer'] or '—'}"
    )
    kb = kb_digits("val", allow_dot=(st["stage"] == "weight"))
    await query.edit_message_text(text, reply_markup=kb)


async def on_menu_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if chat_id not in SESSIONS:
        await query.answer("Сессия не найдена, нажми /start")
        return
    await query.answer()
    st = SESSIONS[chat_id]
    idx = int(query.data.split(":")[1])
    st["ex_idx"] = idx
    group, exercise = current_exercise(chat_id)
    done = sets_done_for(chat_id, st["session_date"], st["day"], exercise)
    if done >= SETS_PER_EXERCISE:
        await show_menu(query, chat_id, note=f"«{exercise}» уже отмечено на {SETS_PER_EXERCISE}/{SETS_PER_EXERCISE} подходов. ")
        return
    st["set_no"] = done + 1
    st["stage"] = "weight"
    st["buffer"] = ""
    st["weight"] = None
    await ask_current(query, chat_id)


async def on_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if chat_id not in SESSIONS:
        await query.answer("Сессия не найдена, нажми /start")
        return
    await query.answer()
    st = SESSIONS[chat_id]
    action = query.data.split(":")[1]

    if action == "menu":
        st["stage"] = "menu"
        await show_menu(query, chat_id)
        return

    if action == "finish":
        del SESSIONS[chat_id]
        await query.edit_message_text(
            "Тренировка записана. Отличная работа! 💪\nНажми /start для следующей."
        )
        return

    if action == "skipset":
        # Пропустить текущий подход без сохранения, перейти к следующему.
        st["buffer"] = ""
        st["weight"] = None
        st["stage"] = "weight"
        if st["set_no"] < SETS_PER_EXERCISE:
            st["set_no"] += 1
            await ask_current(query, chat_id)
        else:
            await show_menu(query, chat_id)
        return


async def on_digit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if chat_id not in SESSIONS:
        await query.answer("Сессия не найдена, нажми /start")
        return
    st = SESSIONS[chat_id]
    _, val = query.data.split(":")

    if val == "done":
        if not st["buffer"]:
            await query.answer("Сначала введи число")
            return
        await query.answer()
        if st["stage"] == "weight":
            st["weight"] = float(st["buffer"])
            st["buffer"] = ""
            st["stage"] = "reps"
            await ask_current(query, chat_id)
        else:
            reps = int(st["buffer"])
            group, exercise = current_exercise(chat_id)
            save_set(
                chat_id, st["session_date"], st["day"], group, exercise,
                st["set_no"], st["weight"], reps,
            )
            st["buffer"] = ""
            st["stage"] = "weight"
            st["weight"] = None
            if st["set_no"] < SETS_PER_EXERCISE:
                st["set_no"] += 1
                await ask_current(query, chat_id)
            else:
                await show_menu(query, chat_id, note=f"«{exercise}» готово ✅. ")
        return

    await query.answer()
    if val == "⌫":
        st["buffer"] = st["buffer"][:-1]
    elif val == "." and ("." in st["buffer"] or st["stage"] == "reps"):
        pass
    else:
        st["buffer"] += val
    await ask_current(query, chat_id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SESSIONS.pop(update.effective_chat.id, None)
    await update.message.reply_text("Ок, отменил текущий ввод. /start — начать заново.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_all(chat_id)
    SESSIONS.pop(chat_id, None)
    await update.message.reply_text("Все твои записи тренировок удалены. /start — начать заново.")


# --------------------------------------------------------------------------
# HTTP-эндпоинт экспорта для синхронизации с xlsx
# --------------------------------------------------------------------------
app = FastAPI()


@app.get("/export")
def export(token: str = Query(...)):
    if token != EXPORT_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")
    return {"rows": all_rows()}


@app.get("/health")
def health():
    return {"ok": True}


def run_http():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CallbackQueryHandler(on_start_day, pattern=r"^startday:"))
    application.add_handler(CallbackQueryHandler(on_menu_pick, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(on_nav, pattern=r"^nav:"))
    application.add_handler(CallbackQueryHandler(on_digit, pattern=r"^val:"))

    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()

    application.run_polling()


if __name__ == "__main__":
    main()
