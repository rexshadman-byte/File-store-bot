import asyncio
import html
import logging
import os
import re
import sqlite3
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.request import HTTPXRequest

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9054260826:AAEkWpzLo7Gyu6nBwfD2ORTEhCDOzmbbYEg").strip()
OWNER_ID = 8574277447
OWNER_USERNAME = "rexofficial_owner"
HOW_TO_CHANNEL = "https://t.me/rexshadmanapi"
DB_PATH = os.getenv("DB_PATH", "rex_file_store.db")
AUTO_DELETE_SECONDS = 600
PORT = int(os.getenv("PORT", "10000"))


# Render Web Service health endpoint. This runs beside Telegram polling.
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"REX FILE STORE BOT is online"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)
    return server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("rex-file-store")

# Reply-keyboard labels
CONTACT = "📩 CONTACT OWNER"
HOWTO = "📁 HOW TO GET FILE"
ADD_FILE = "📤 ADD FILE"
MY_FILES = "🗂 FILE LIST"
SHARE = "🔗 CREATE LINK"
REMOVE_FILE = "🗑 DELETE FILE"
BROADCAST = "📢 BROADCAST"
ADD_CHANNEL = "➕ ADD FORCE CHANNEL"
CHANNEL_LIST = "📋 CHANNEL LIST"
REMOVE_CHANNEL = "➖ REMOVE CHANNEL"
ADD_ADMIN = "👑 ADD ADMIN"
ADMIN_LIST = "👥 ADMIN LIST"
REMOVE_ADMIN = "❌ REMOVE ADMIN"
USER_MENU = "🏠 USER MENU"
ADMIN_PANEL = "⚙️ ADMIN PANEL"
CANCEL = "❌ CANCEL"
BACK = "⬅️ BACK"

# ===================== DATABASE =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS channels(channel TEXT PRIMARY KEY)")
        con.execute("""CREATE TABLE IF NOT EXISTS files(
            file_key TEXT PRIMARY KEY, file_id TEXT NOT NULL, file_type TEXT NOT NULL,
            owner_id INTEGER NOT NULL, file_name TEXT NOT NULL,
            link_label TEXT NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (OWNER_ID,))


def is_admin(uid):
    with db() as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None


def remember_user(uid):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users VALUES(?,?)",
            (uid, datetime.now(timezone.utc).isoformat())
        )


def channels():
    with db() as con:
        return [r["channel"] for r in con.execute("SELECT channel FROM channels ORDER BY channel")]


def get_file(key):
    with db() as con:
        row = con.execute("SELECT * FROM files WHERE file_key=?", (key,)).fetchone()
        return dict(row) if row else None


def get_files(uid):
    with db() as con:
        if uid == OWNER_ID:
            rows = con.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM files WHERE owner_id=? ORDER BY created_at DESC", (uid,)
            ).fetchall()
        return [dict(r) for r in rows]


# ===================== BOTTOM KEYBOARDS =====================
def user_keyboard():
    # Persistent keyboard at the bottom of Telegram chat.
    return ReplyKeyboardMarkup(
        [[KeyboardButton(CONTACT)], [KeyboardButton(HOWTO)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Select an option"
    )


def admin_keyboard(uid):
    rows = [
        [ADD_FILE, MY_FILES],
        [SHARE, REMOVE_FILE],
        [BROADCAST, ADD_CHANNEL],
        [CHANNEL_LIST, REMOVE_CHANNEL],
    ]
    if uid == OWNER_ID:
        rows += [[ADD_ADMIN, ADMIN_LIST], [REMOVE_ADMIN]]
    rows += [[USER_MENU]]
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="REX Admin Control Panel"
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [[CANCEL]], resize_keyboard=True, is_persistent=True
    )


def back_keyboard(uid):
    return ReplyKeyboardMarkup(
        [[BACK], [USER_MENU if not is_admin(uid) else ADMIN_PANEL]],
        resize_keyboard=True, is_persistent=True
    )


def welcome_text(user):
    return (
        "👑 <b>REX FILE STORE BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Welcome, <b>{html.escape(user.first_name or 'User')}</b>!\n\n"
        "Select an option from the keyboard below."
    )


def panel_text():
    return (
        "👑 <b>REX ADMIN CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "All management tools are available in the bottom keyboard."
    )


# ===================== FORCE JOIN =====================
async def is_subscribed(bot, uid):
    if is_admin(uid):
        return True
    for channel in channels():
        try:
            member = await bot.get_chat_member(channel, uid)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            log.exception("Membership check failed for %s", channel)
            return False
    return True


async def join_prompt(message, context):
    rows = []
    for i, channel in enumerate(channels(), 1):
        try:
            chat = await context.bot.get_chat(channel)
            url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
            if url:
                rows.append([InlineKeyboardButton(f"JOIN CHANNEL {i}", url=url)])
        except Exception:
            log.exception("Cannot create join button for %s", channel)
    rows.append([InlineKeyboardButton("VERIFY JOIN", callback_data="verify_join")])
    await message.reply_text(
        "🔒 <b>CHANNEL SUBSCRIPTION REQUIRED</b>\n\n"
        "Please join all required channels, then press VERIFY JOIN.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await is_subscribed(context.bot, q.from_user.id):
        if is_admin(q.from_user.id):
            await q.message.reply_text(
                panel_text(), parse_mode="HTML",
                reply_markup=admin_keyboard(q.from_user.id)
            )
        else:
            await q.message.reply_text(
                welcome_text(q.from_user), parse_mode="HTML",
                reply_markup=user_keyboard()
            )
    else:
        await q.answer("Please join all required channels first.", show_alert=True)


# ===================== FILE DELIVERY =====================
async def delete_after_10m(bot, chat_id, message_id):
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        log.info("Auto-delete could not remove message %s", message_id)


async def deliver_file(message, context, key):
    f = get_file(key)
    if not f:
        await message.reply_text("❌ This file link is invalid or the file has been removed.")
        return
    if not await is_subscribed(context.bot, message.from_user.id):
        await join_prompt(message, context)
        return

    notice = await message.reply_text(
        "⚠️ THIS FILE WILL BE DELETED WITHIN 10 MINUTES.\n"
        "PLEASE SAVE IT QUICKLY."
    )
    try:
        args = {"chat_id": message.chat_id, "caption": f["file_name"]}
        if f["file_type"] == "document":
            sent = await context.bot.send_document(document=f["file_id"], **args)
        elif f["file_type"] == "photo":
            sent = await context.bot.send_photo(photo=f["file_id"], **args)
        elif f["file_type"] == "video":
            sent = await context.bot.send_video(video=f["file_id"], **args)
        elif f["file_type"] == "audio":
            sent = await context.bot.send_audio(audio=f["file_id"], **args)
        else:
            await notice.edit_text("Unsupported file type.")
            return
        asyncio.create_task(delete_after_10m(context.bot, message.chat_id, sent.message_id))
        asyncio.create_task(delete_after_10m(context.bot, message.chat_id, notice.message_id))
    except Exception:
        log.exception("File delivery failed")
        await notice.edit_text("❌ File delivery failed. Please try again later.")


# ===================== START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    remember_user(user.id)

    if context.args:
        arg = context.args[0]
        if arg != "verify":
            await deliver_file(update.message, context, arg)
            return

    if not await is_subscribed(context.bot, user.id):
        await join_prompt(update.message, context)
        return

    if is_admin(user.id):
        await update.message.reply_text(
            panel_text(), parse_mode="HTML", reply_markup=admin_keyboard(user.id)
        )
    else:
        await update.message.reply_text(
            welcome_text(user), parse_mode="HTML", reply_markup=user_keyboard()
        )


# ===================== MENU ROUTER =====================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    user = update.effective_user
    uid = user.id
    remember_user(uid)
    text = (update.message.text or "").strip()

    if text == CANCEL:
        context.user_data.clear()
        await update.message.reply_text(
            "Action cancelled.",
            reply_markup=admin_keyboard(uid) if is_admin(uid) else user_keyboard()
        )
        return

    if text == BACK or text == ADMIN_PANEL:
        context.user_data.clear()
        if is_admin(uid):
            await update.message.reply_text(panel_text(), parse_mode="HTML", reply_markup=admin_keyboard(uid))
        else:
            await update.message.reply_text(welcome_text(user), parse_mode="HTML", reply_markup=user_keyboard())
        return

    # Active multi-step tasks
    stage = context.user_data.get("stage")
    # Numeric selections are handled by selection_router (next handler group).
    if stage in ("select_share", "select_delete", "remove_channel", "remove_admin"):
        return
    if stage == "file_upload":
        await receive_upload(update, context)
        return
    if stage == "file_title":
        title = text
        if not title or len(title) > 180:
            await update.message.reply_text("Please enter a file name (1–180 characters).")
            return
        context.user_data["pending"]["file_name"] = title
        context.user_data["stage"] = "link_label"
        await update.message.reply_text("Enter the hyperlink text users should see.")
        return
    if stage == "link_label":
        label = text
        if not label or len(label) > 100:
            await update.message.reply_text("Please enter hyperlink text (1–100 characters).")
            return
        context.user_data["pending"]["link_label"] = label
        context.user_data["stage"] = "link_suffix"
        await update.message.reply_text(
            "Enter your custom link suffix (letters, numbers, _ or -), or type AUTO."
        )
        return
    if stage == "link_suffix":
        suffix = text
        if suffix.upper() == "AUTO":
            key = uuid.uuid4().hex[:12]
        elif re.fullmatch(r"[A-Za-z0-9_-]{1,48}", suffix):
            key = suffix
        else:
            await update.message.reply_text("Invalid suffix. Use 1–48 letters/numbers/_/- or AUTO.")
            return
        p = context.user_data["pending"]
        with db() as con:
            if con.execute("SELECT 1 FROM files WHERE file_key=?", (key,)).fetchone():
                await update.message.reply_text("That suffix is already in use. Enter another one.")
                return
            con.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                (key, p["file_id"], p["file_type"], uid, p["file_name"],
                 p["link_label"], datetime.now(timezone.utc).isoformat())
            )
        me = await context.bot.get_me()
        url = f"https://t.me/{me.username}?start={key}"
        context.user_data.clear()
        await update.message.reply_text(
            "👑 FILE SAVED SUCCESSFULLY\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"FILE NAME: {p['file_name']}\n"
            f"CUSTOM SUFFIX: {key}\n\n"
            f"HYPERLINK: {p['link_label']}\n{url}\n\n"
            f"HTML LINK:\n<a href=\"{url}\">{html.escape(p['link_label'])}</a>",
            parse_mode="HTML", reply_markup=admin_keyboard(uid)
        )
        return
    if stage == "add_channel":
        channel = text
        if not (channel.startswith("@") or channel.startswith("-100")):
            await update.message.reply_text("Send a public @username or private -100 chat ID.")
            return
        try:
            chat = await context.bot.get_chat(channel)
            bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if bot_member.status not in ("administrator", "creator"):
                await update.message.reply_text("Please make the bot an administrator in that channel first.")
                return
        except Exception:
            await update.message.reply_text("Channel verification failed. Check the ID and bot admin permission.")
            return
        with db() as con:
            con.execute("INSERT OR IGNORE INTO channels(channel) VALUES(?)", (channel,))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Force-join channel added: {html.escape(channel)}",
            parse_mode="HTML", reply_markup=admin_keyboard(uid)
        )
        return
    if stage == "add_admin":
        if uid != OWNER_ID:
            context.user_data.clear()
            return
        try:
            new_id = int(text)
            if new_id <= 0 or new_id == OWNER_ID:
                raise ValueError
            with db() as con:
                con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (new_id,))
            context.user_data.clear()
            await update.message.reply_text("✅ Admin added successfully.", reply_markup=admin_keyboard(uid))
        except ValueError:
            await update.message.reply_text("Please send a valid numeric Telegram User ID.")
        return
    if stage == "broadcast":
        if not is_admin(uid):
            context.user_data.clear()
            return
        with db() as con:
            user_ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users")]
        sent = failed = 0
        for target in user_ids:
            try:
                if update.message.photo:
                    await context.bot.send_photo(target, update.message.photo[-1].file_id, caption=update.message.caption or "")
                elif update.message.video:
                    await context.bot.send_video(target, update.message.video.file_id, caption=update.message.caption or "")
                elif update.message.document:
                    await context.bot.send_document(target, update.message.document.file_id, caption=update.message.caption or "")
                else:
                    await context.bot.send_message(target, text)
                sent += 1
            except Exception:
                failed += 1
        context.user_data.clear()
        await update.message.reply_text(
            f"BROADCAST COMPLETED\nSent: {sent}\nFailed: {failed}",
            reply_markup=admin_keyboard(uid)
        )
        return

    # Public user options: only these two menu choices.
    if text == CONTACT:
        await update.message.reply_text(
            "CONTACT OWNER\nTap the button below to open the owner's inbox.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 OPEN OWNER CHAT", url=f"https://t.me/{OWNER_USERNAME}")
            ]])
        )
        return
    if text == HOWTO:
        await update.message.reply_text(
            "HOW TO GET FILE\nOpen our channel using the button below.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 OPEN CHANNEL", url=HOW_TO_CHANNEL)
            ]])
        )
        return

    # Never expose admin actions to ordinary users.
    if not is_admin(uid):
        await update.message.reply_text(
            welcome_text(user), parse_mode="HTML", reply_markup=user_keyboard()
        )
        return

    # Admin actions
    if text == ADD_FILE:
        context.user_data["stage"] = "file_upload"
        await update.message.reply_text(
            "📤 Send a Document, Photo, Video or Audio file now.",
            reply_markup=cancel_keyboard()
        )
    elif text == MY_FILES:
        files = get_files(uid)
        if not files:
            await update.message.reply_text("No files have been uploaded yet.", reply_markup=admin_keyboard(uid))
            return
        lines = ["🗂 YOUR FILES"]
        for f in files[:50]:
            lines.append(f"• {f['file_name']}  |  Key: {f['file_key']}")
        await update.message.reply_text("\n".join(lines), reply_markup=admin_keyboard(uid))
    elif text in (SHARE, REMOVE_FILE):
        files = get_files(uid)
        if not files:
            await update.message.reply_text("No files available.", reply_markup=admin_keyboard(uid))
            return
        context.user_data["stage"] = "select_share" if text == SHARE else "select_delete"
        context.user_data["file_choices"] = [f["file_key"] for f in files]
        lines = ["Select a file by sending its number:"]
        for i, f in enumerate(files[:50], 1):
            lines.append(f"{i}. {f['file_name']} [{f['file_key']}]")
        await update.message.reply_text("\n".join(lines), reply_markup=cancel_keyboard())
    elif stage in ("select_share", "select_delete"):
        pass
    elif text == BROADCAST:
        context.user_data["stage"] = "broadcast"
        await update.message.reply_text("Send the broadcast text or a photo/video/document with caption.", reply_markup=cancel_keyboard())
    elif text == ADD_CHANNEL:
        context.user_data["stage"] = "add_channel"
        await update.message.reply_text("Send a public @channelusername or private -100 chat ID. The bot must be an admin there.", reply_markup=cancel_keyboard())
    elif text == CHANNEL_LIST:
        chs = channels()
        body = "\n".join(f"{i}. {ch}" for i, ch in enumerate(chs, 1)) or "No force-join channels configured."
        await update.message.reply_text("📋 FORCE-JOIN CHANNELS\n\n" + body, reply_markup=admin_keyboard(uid))
    elif text == REMOVE_CHANNEL:
        chs = channels()
        if not chs:
            await update.message.reply_text("No channels to remove.", reply_markup=admin_keyboard(uid))
        else:
            context.user_data["stage"] = "remove_channel"
            context.user_data["channel_choices"] = chs
            await update.message.reply_text(
                "Send the number of the channel to remove:\n" +
                "\n".join(f"{i}. {c}" for i, c in enumerate(chs, 1)),
                reply_markup=cancel_keyboard()
            )
    elif text == ADD_ADMIN and uid == OWNER_ID:
        context.user_data["stage"] = "add_admin"
        await update.message.reply_text("Send the new Admin's numeric Telegram User ID.", reply_markup=cancel_keyboard())
    elif text == ADMIN_LIST and uid == OWNER_ID:
        with db() as con:
            admins = [r["user_id"] for r in con.execute("SELECT user_id FROM admins ORDER BY user_id")]
        body = "\n".join(f"• {a} — {'OWNER' if a == OWNER_ID else 'ADMIN'}" for a in admins)
        await update.message.reply_text("👑 ADMIN LIST\n\n" + body, reply_markup=admin_keyboard(uid))
    elif text == REMOVE_ADMIN and uid == OWNER_ID:
        with db() as con:
            admins = [r["user_id"] for r in con.execute("SELECT user_id FROM admins WHERE user_id != ?", (OWNER_ID,))]
        if not admins:
            await update.message.reply_text("No additional admins to remove.", reply_markup=admin_keyboard(uid))
        else:
            context.user_data["stage"] = "remove_admin"
            context.user_data["admin_choices"] = admins
            await update.message.reply_text(
                "Send the number of the Admin to remove:\n" +
                "\n".join(f"{i}. {a}" for i, a in enumerate(admins, 1)),
                reply_markup=cancel_keyboard()
            )
    elif text == USER_MENU:
        await update.message.reply_text(welcome_text(user), parse_mode="HTML", reply_markup=user_keyboard())
    else:
        await update.message.reply_text(panel_text(), parse_mode="HTML", reply_markup=admin_keyboard(uid))


# ===================== FILE / LIST SELECTION =====================
async def receive_upload(update, context):
    msg = update.message
    p = None
    if msg.document:
        p = (msg.document.file_id, "document")
    elif msg.photo:
        p = (msg.photo[-1].file_id, "photo")
    elif msg.video:
        p = (msg.video.file_id, "video")
    elif msg.audio:
        p = (msg.audio.file_id, "audio")
    if not p:
        await msg.reply_text("Please send a Document, Photo, Video or Audio file.")
        return
    context.user_data["pending"] = {"file_id": p[0], "file_type": p[1]}
    context.user_data["stage"] = "file_title"
    await msg.reply_text("✅ File received. Now enter the custom file name.")


async def selection_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    stage = context.user_data.get("stage")
    if stage not in ("select_share", "select_delete", "remove_channel", "remove_admin"):
        return
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please send a valid number.")
        return
    choices_key = {
        "select_share": "file_choices", "select_delete": "file_choices",
        "remove_channel": "channel_choices", "remove_admin": "admin_choices"
    }[stage]
    choices = context.user_data.get(choices_key, [])
    if not 1 <= n <= len(choices):
        await update.message.reply_text("Number out of range. Try again.")
        return
    selected = choices[n - 1]
    context.user_data.clear()

    if stage == "select_share":
        f = get_file(selected)
        me = await context.bot.get_me()
        url = f"https://t.me/{me.username}?start={selected}"
        await update.message.reply_text(
            f"🔗 {f['file_name']}\n\n"
            f'<a href="{url}">{html.escape(f["link_label"])}</a>\n\n'
            f"Direct link:\n{url}",
            parse_mode="HTML", reply_markup=admin_keyboard(uid)
        )
    elif stage == "select_delete":
        with db() as con:
            con.execute("DELETE FROM files WHERE file_key=?", (selected,))
        await update.message.reply_text("✅ File deleted.", reply_markup=admin_keyboard(uid))
    elif stage == "remove_channel":
        with db() as con:
            con.execute("DELETE FROM channels WHERE channel=?", (selected,))
        await update.message.reply_text("✅ Force-join channel removed.", reply_markup=admin_keyboard(uid))
    elif stage == "remove_admin" and uid == OWNER_ID:
        with db() as con:
            con.execute("DELETE FROM admins WHERE user_id=? AND user_id!=?", (selected, OWNER_ID))
        await update.message.reply_text("✅ Admin removed.", reply_markup=admin_keyboard(uid))


# ===================== RUN =====================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    init_db()
    start_health_server()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(HTTPXRequest(connect_timeout=30, read_timeout=60, write_timeout=60, pool_timeout=30))
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_join$"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, menu_router), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, selection_router), group=1)
    log.info("REX FILE STORE BOT is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
