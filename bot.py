import os
import json
import asyncio
import signal
import logging
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopePeer, InputPeerUser
from telethon.errors import FloodWaitError

import db

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Clients ─────────────────────────────────────────────────────

user = TelegramClient("user_session", API_ID, API_HASH)
bot = TelegramClient("bot_session", API_ID, API_HASH)

# Storage for "show more" missed vacancies per user
# {user_id: [list of remaining formatted messages]}
pending_more: dict[int, list[str]] = {}

MAX_MISSED = 30
DIGEST_PAGE = 15


# ── Catch-up: fetch missed messages ───────────────────────────

async def catch_up():
    """Fetch messages posted while the bot was offline and send digests."""
    last_check = db.get_last_check_time()
    if last_check is None:
        log.info("Первый запуск — пропускаем catch-up.")
        db.save_last_check_time()
        return

    channels = db.get_channels()
    users_with_keywords = db.get_all_users_with_keywords()
    if not channels or not users_with_keywords:
        db.save_last_check_time()
        return

    log.info("Catch-up: ищем сообщения после %s по %d каналам...", last_check.isoformat(), len(channels))

    # Collect matches per user: {user_id: [(channel_name, link, matched_kws, text_preview)]}
    user_matches: dict[int, list[tuple]] = {}

    for ch_username in channels:
        try:
            entity = await user.get_entity(ch_username)
        except Exception as e:
            log.warning("Catch-up: не удалось найти канал @%s: %s", ch_username, e)
            continue

        try:
            async for msg in user.iter_messages(entity, offset_date=last_check, reverse=True):
                if msg.date.replace(tzinfo=None) <= last_check:
                    continue
                text = (msg.raw_text or "").lower()
                if not text:
                    continue

                chat_username = getattr(entity, "username", None)
                channel_name = getattr(entity, "title", None) or chat_username or ch_username

                for u in users_with_keywords:
                    uid = u["user_id"]
                    matched = [kw for kw in u["keywords"] if kw.lower() in text]
                    if not matched:
                        continue

                    if uid not in user_matches:
                        user_matches[uid] = []
                    if len(user_matches[uid]) >= MAX_MISSED:
                        continue

                    # Build link
                    is_bot_chat = getattr(entity, "bot", False)
                    if chat_username and not is_bot_chat:
                        link = f"https://t.me/{chat_username}/{msg.id}"
                    elif chat_username and is_bot_chat:
                        link = f"https://t.me/{chat_username}"
                    elif not is_bot_chat:
                        link = f"https://t.me/c/{abs(entity.id)}/{msg.id}"
                    else:
                        link = ""

                    preview = (msg.raw_text or "")[:100]
                    if len(msg.raw_text or "") > 100:
                        preview += "..."

                    user_matches[uid].append((channel_name, link, matched, preview))

        except FloodWaitError as e:
            log.warning("Catch-up: FloodWait %d сек на канале @%s, пропускаем.", e.seconds, ch_username)
            await asyncio.sleep(min(e.seconds, 10))
            continue
        except Exception as e:
            log.warning("Catch-up: ошибка на канале @%s: %s", ch_username, e)
            continue

        # Small delay between channels to avoid flood
        await asyncio.sleep(0.5)

    # Send digests
    for uid, matches in user_matches.items():
        total = len(matches)
        first_page = matches[:DIGEST_PAGE]
        remaining = matches[DIGEST_PAGE:]

        header = f"📋 **Пропущенные вакансии ({total} шт.)**\n{'─' * 30}\n\n"
        lines = []
        for i, (ch_name, link, kws, preview) in enumerate(first_page, 1):
            entry = f"**{i}.** 📢 {ch_name}\n🔑 {', '.join(kws)}\n💬 {preview}\n"
            if link:
                entry += f"🔗 [Открыть]({link})\n"
            lines.append(entry)

        body = header + "\n".join(lines)

        if remaining:
            body += f"\n{'─' * 30}\nЕщё {len(remaining)} вакансий. Нажми /more чтобы посмотреть."
            pending_more[uid] = remaining

        try:
            await bot.send_message(uid, body, link_preview=False)
            log.info("Catch-up: отправлен дайджест пользователю %s (%d вакансий)", uid, total)
        except Exception as e:
            log.error("Catch-up: ошибка отправки пользователю %s: %s", uid, e)

    log.info("Catch-up завершён. Найдено совпадений для %d пользователей.", len(user_matches))
    db.save_last_check_time()


# ── Periodic save of last_check_time ──────────────────────────

async def periodic_save():
    """Save last_check_time every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        db.save_last_check_time()
        log.debug("last_check_time сохранён.")


# ── Channel monitor (userbot) ──────────────────────────────────

@user.on(events.NewMessage)
async def on_channel_message(event):
    """Check new messages in monitored channels against all users' keywords."""
    chat = await event.get_chat()
    chat_username = getattr(chat, "username", None)
    chat_id = event.chat_id

    channels = db.get_channels()
    if not channels:
        return

    # Check if this channel is in the admin pool
    is_tracked = False
    for ch in channels:
        ch_str = ch.lower().lstrip("@")
        if chat_username and chat_username.lower() == ch_str:
            is_tracked = True
            break
        if str(chat_id) == ch_str:
            is_tracked = True
            break

    if not is_tracked:
        return

    text = (event.raw_text or "").lower()
    if not text:
        return

    log.info("Сообщение из канала: %s (id=%s)", chat_username, chat_id)

    # Update last_check_time on every processed message
    db.save_last_check_time()

    # Check keywords for ALL users
    users_with_keywords = db.get_all_users_with_keywords()

    for u in users_with_keywords:
        uid = u["user_id"]
        keywords = u["keywords"]
        matched = [kw for kw in keywords if kw.lower() in text]
        if not matched:
            continue

        # Build notification
        channel_name = getattr(chat, "title", None) or chat_username or str(chat_id)
        is_bot_chat = getattr(chat, "bot", False)
        if chat_username and not is_bot_chat:
            link = f"https://t.me/{chat_username}/{event.id}"
        elif chat_username and is_bot_chat:
            link = f"https://t.me/{chat_username}"
        elif not is_bot_chat:
            link = f"https://t.me/c/{abs(chat_id)}/{event.id}"
        else:
            link = ""

        header = (
            f"🔔 **Найдено совпадение!**\n"
            f"📢 Канал: **{channel_name}**\n"
            f"🔑 Слова: {', '.join(matched)}\n"
        )
        if link:
            header += f"🔗 [Открыть сообщение]({link})\n"
        header += f"{'─' * 30}"

        try:
            # Send header + original message text via bot (not userbot)
            preview = (event.raw_text or "")[:500]
            if len(event.raw_text or "") > 500:
                preview += "..."
            full_msg = header + "\n\n" + preview
            await bot.send_message(uid, full_msg, link_preview=False)
            log.info("Отправлено пользователю %s, слова: %s", uid, matched)
        except Exception as e:
            log.error("Ошибка отправки пользователю %s: %s", uid, e)


# ── Allowed commands per role ─────────────────────────────────

PUBLIC_COMMANDS = {
    "/start", "/help", "/keywords", "/add_keyword", "/remove_keyword",
    "/suggest_channel", "/status", "/more",
}
ADMIN_COMMANDS = {
    "/channels", "/add_channel", "/remove_channel",
    "/approve", "/reject", "/users", "/suggestions",
}


@bot.on(events.NewMessage(pattern=r"^/"))
async def command_guard(event):
    """Block any command not in the user's allowed set."""
    text = (event.raw_text or "").strip()
    cmd = text.split()[0].split("@")[0].lower()  # handle /cmd@botname

    if event.sender_id == ADMIN_ID:
        allowed = PUBLIC_COMMANDS | ADMIN_COMMANDS
    else:
        allowed = PUBLIC_COMMANDS

    if cmd not in allowed:
        await event.respond("⚠️ Неизвестная команда. Напиши /help для списка команд.")
        raise events.StopPropagation


# ── Bot commands — public (any user) ──────────────────────────

@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    sender = await event.get_sender()
    db.register_user(
        event.sender_id,
        getattr(sender, "username", None),
        getattr(sender, "first_name", None),
    )
    await event.respond(
        "👋 **Привет! Я бот для мониторинга вакансий в Telegram-каналах.**\n\n"
        "Я слежу за десятками каналов с вакансиями и присылаю тебе "
        "только те, которые подходят под твои ключевые слова.\n\n"
        "**Как начать за 1 минуту:**\n"
        "1️⃣ Добавь ключевые слова — например:\n"
        "   `/add_keyword Python`\n"
        "   `/add_keyword frontend`\n"
        "   `/add_keyword remote`\n\n"
        "2️⃣ Готово! Я буду присылать подходящие вакансии.\n\n"
        "**Команды:**\n"
        "/keywords — твои ключевые слова\n"
        "/add_keyword `слово` — добавить слово\n"
        "/remove_keyword `слово` — удалить слово\n"
        "/suggest_channel `@канал` — предложить канал\n"
        "/status — текущий статус\n"
        "/help — справка"
    )


@bot.on(events.NewMessage(pattern="/help"))
async def cmd_help(event):
    await event.respond(
        "📖 **Справка**\n\n"
        "Я мониторю каналы с вакансиями и присылаю тебе сообщения, "
        "в которых есть твои ключевые слова.\n\n"
        "**Что нужно сделать:**\n"
        "Добавь ключевые слова — названия технологий, языков, "
        "должностей, которые тебя интересуют.\n\n"
        "**Примеры ключевых слов:**\n"
        "• `Python` — все вакансии с Python\n"
        "• `junior` — джуниор-позиции\n"
        "• `remote` — удалённая работа\n"
        "• `Flutter` — мобильная разработка\n\n"
        "**Команды:**\n"
        "/keywords — посмотреть свои слова\n"
        "/add_keyword `слово` — добавить\n"
        "/remove_keyword `слово` — удалить\n"
        "/suggest_channel `@канал` — предложить канал для мониторинга\n"
        "/status — статус мониторинга"
    )


@bot.on(events.NewMessage(pattern="/keywords$"))
async def cmd_keywords(event):
    keywords = db.get_user_keywords(event.sender_id)
    if not keywords:
        await event.respond(
            "🔑 У тебя пока нет ключевых слов.\n\n"
            "Добавь первое слово:\n"
            "`/add_keyword Python`"
        )
        return
    lines = "\n".join(f"  • {kw}" for kw in keywords)
    await event.respond(f"🔑 **Твои ключевые слова:**\n{lines}")


@bot.on(events.NewMessage(pattern=r"/add_keyword\s+(.+)"))
async def cmd_add_keyword(event):
    keyword = event.pattern_match.group(1).strip()
    if not keyword:
        await event.respond("⚠️ Укажи слово: `/add_keyword Python`")
        return
    # Register user if not yet (in case they skipped /start)
    sender = await event.get_sender()
    db.register_user(
        event.sender_id,
        getattr(sender, "username", None),
        getattr(sender, "first_name", None),
    )
    if db.add_user_keyword(event.sender_id, keyword):
        count = len(db.get_user_keywords(event.sender_id))
        await event.respond(
            f"✅ Слово **{keyword}** добавлено!\n"
            f"Всего слов: {count}\n\n"
            f"Теперь я буду присылать вакансии с этим словом."
        )
    else:
        await event.respond(f"⚠️ Слово **{keyword}** уже есть в твоём списке.")


@bot.on(events.NewMessage(pattern=r"/remove_keyword\s+(.+)"))
async def cmd_remove_keyword(event):
    keyword = event.pattern_match.group(1).strip()
    if db.remove_user_keyword(event.sender_id, keyword):
        await event.respond(f"🗑 Слово **{keyword}** удалено.")
    else:
        await event.respond(f"⚠️ Слово **{keyword}** не найдено в твоём списке.")


@bot.on(events.NewMessage(pattern=r"/suggest_channel\s+(.+)"))
async def cmd_suggest_channel(event):
    channel = event.pattern_match.group(1).strip().lstrip("@")
    if not channel:
        await event.respond("⚠️ Укажи канал: `/suggest_channel @channel_name`")
        return

    # Check if channel already in pool
    existing = db.get_channels()
    if channel.lower() in [c.lower() for c in existing]:
        await event.respond(f"ℹ️ Канал **@{channel}** уже есть в базе. Он мониторится!")
        return

    sender = await event.get_sender()
    db.register_user(
        event.sender_id,
        getattr(sender, "username", None),
        getattr(sender, "first_name", None),
    )
    suggestion_id = db.add_suggestion(event.sender_id, channel)
    await event.respond(
        f"📬 Канал **@{channel}** отправлен на рассмотрение!\n"
        f"Администратор проверит и добавит его в ближайшее время."
    )

    # Notify admin
    sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", "Неизвестный")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 **Предложение канала**\n"
            f"От: @{sender_name} (id: {event.sender_id})\n"
            f"Канал: **@{channel}**\n\n"
            f"Добавить: `/approve {suggestion_id}`\n"
            f"Отклонить: `/reject {suggestion_id}`",
        )
    except Exception as e:
        log.error("Не удалось уведомить админа: %s", e)


@bot.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    keywords = db.get_user_keywords(event.sender_id)
    channels = db.get_channels()
    kw_count = len(keywords)
    ch_count = len(channels)

    if kw_count == 0:
        status_text = "⏸ Добавь ключевые слова, чтобы начать получать вакансии."
    else:
        status_text = "✅ Мониторинг активен! Вакансии будут приходить сюда."

    await event.respond(
        f"📊 **Твой статус**\n"
        f"Каналов в базе: {ch_count}\n"
        f"Твоих ключевых слов: {kw_count}\n"
        f"{status_text}"
    )


@bot.on(events.NewMessage(pattern="/more$"))
async def cmd_more(event):
    remaining = pending_more.get(event.sender_id)
    if not remaining:
        await event.respond("📭 Нет больше пропущенных вакансий.")
        return

    page = remaining[:DIGEST_PAGE]
    left = remaining[DIGEST_PAGE:]

    lines = []
    for i, (ch_name, link, kws, preview) in enumerate(page, 1):
        entry = f"**{i}.** 📢 {ch_name}\n🔑 {', '.join(kws)}\n💬 {preview}\n"
        if link:
            entry += f"🔗 [Открыть]({link})\n"
        lines.append(entry)

    body = f"📋 **Ещё вакансии ({len(page)} шт.)**\n{'─' * 30}\n\n" + "\n".join(lines)

    if left:
        body += f"\n{'─' * 30}\nЕщё {len(left)} вакансий. Нажми /more чтобы посмотреть."
        pending_more[event.sender_id] = left
    else:
        body += f"\n{'─' * 30}\n✅ Все пропущенные вакансии показаны."
        pending_more.pop(event.sender_id, None)

    await event.respond(body, link_preview=False)


# ── Bot commands — admin only ─────────────────────────────────

@bot.on(events.NewMessage(pattern="/channels$"))
async def cmd_channels(event):
    if event.sender_id != ADMIN_ID:
        return
    channels = db.get_channels()
    if not channels:
        await event.respond("📢 Список каналов пуст. Добавьте: /add_channel @username")
        return
    lines = "\n".join(f"  • @{ch}" for ch in channels)
    await event.respond(f"📢 **Каналы в пуле ({len(channels)}):**\n{lines}")


@bot.on(events.NewMessage(pattern=r"/add_channel\s+(.+)"))
async def cmd_add_channel(event):
    if event.sender_id != ADMIN_ID:
        return
    channel = event.pattern_match.group(1).strip().lstrip("@")
    if db.add_channel(channel):
        await event.respond(f"✅ Канал **@{channel}** добавлен в пул!")
    else:
        await event.respond(f"⚠️ Канал **@{channel}** уже в пуле.")


@bot.on(events.NewMessage(pattern=r"/remove_channel\s+(.+)"))
async def cmd_remove_channel(event):
    if event.sender_id != ADMIN_ID:
        return
    channel = event.pattern_match.group(1).strip().lstrip("@")
    if db.remove_channel(channel):
        await event.respond(f"🗑 Канал **@{channel}** удалён из пула.")
    else:
        await event.respond(f"⚠️ Канал **@{channel}** не найден.")


@bot.on(events.NewMessage(pattern=r"/approve\s+(\d+)"))
async def cmd_approve(event):
    if event.sender_id != ADMIN_ID:
        return
    suggestion_id = int(event.pattern_match.group(1))
    suggestions = db.get_pending_suggestions()
    target = None
    for s in suggestions:
        if s["id"] == suggestion_id:
            target = s
            break
    if not target:
        await event.respond("⚠️ Предложение не найдено или уже обработано.")
        return

    db.add_channel(target["channel"])
    db.update_suggestion_status(suggestion_id, "approved")
    await event.respond(f"✅ Канал **@{target['channel']}** добавлен!")

    # Notify the user who suggested
    try:
        await bot.send_message(
            target["user_id"],
            f"🎉 Твоё предложение одобрено! Канал **@{target['channel']}** "
            f"добавлен в мониторинг.",
        )
    except Exception:
        pass


@bot.on(events.NewMessage(pattern=r"/reject\s+(\d+)"))
async def cmd_reject(event):
    if event.sender_id != ADMIN_ID:
        return
    suggestion_id = int(event.pattern_match.group(1))
    suggestions = db.get_pending_suggestions()
    target = None
    for s in suggestions:
        if s["id"] == suggestion_id:
            target = s
            break
    if not target:
        await event.respond("⚠️ Предложение не найдено или уже обработано.")
        return

    db.update_suggestion_status(suggestion_id, "rejected")
    await event.respond(f"❌ Предложение канала **@{target['channel']}** отклонено.")


@bot.on(events.NewMessage(pattern="/users$"))
async def cmd_users(event):
    if event.sender_id != ADMIN_ID:
        return
    users = db.get_all_users()
    if not users:
        await event.respond("👥 Пользователей пока нет.")
        return
    lines = []
    for u in users:
        name = u.get("username") or u.get("first_name") or str(u["user_id"])
        kw_count = len(db.get_user_keywords(u["user_id"]))
        lines.append(f"  • @{name} — {kw_count} слов")
    await event.respond(f"👥 **Пользователи ({len(users)}):**\n" + "\n".join(lines))


@bot.on(events.NewMessage(pattern="/suggestions$"))
async def cmd_suggestions(event):
    if event.sender_id != ADMIN_ID:
        return
    pending = db.get_pending_suggestions()
    if not pending:
        await event.respond("📬 Нет новых предложений каналов.")
        return
    lines = []
    for s in pending:
        name = s.get("username") or s.get("first_name") or str(s["user_id"])
        lines.append(f"  #{s['id']} — @{s['channel']} (от @{name})")
    await event.respond(
        f"📬 **Предложения ({len(pending)}):**\n" + "\n".join(lines) + "\n\n"
        "Одобрить: `/approve ID`\nОтклонить: `/reject ID`"
    )


# ── Main ────────────────────────────────────────────────────────

async def main():
    # Initialize database
    db.init_db()
    log.info("База данных инициализирована.")

    # Import channels and admin keywords from config.json on first run
    if not db.get_channels() and CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            old_cfg = json.load(f)
        old_channels = old_cfg.get("channels", [])
        if old_channels:
            db.import_channels_from_config(old_channels)
            log.info("Импортировано %d каналов из config.json", len(old_channels))
        # Register admin as user and import their keywords
        db.register_user(ADMIN_ID, None, "Admin")
        old_keywords = old_cfg.get("keywords", [])
        for kw in old_keywords:
            db.add_user_keyword(ADMIN_ID, kw)
        if old_keywords:
            log.info("Импортировано %d keywords админа из config.json", len(old_keywords))

    log.info("Запуск userbot...")
    await user.start()
    log.info("Userbot подключён: %s", (await user.get_me()).first_name)

    log.info("Запуск бота...")
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Бот подключён: @%s", bot_me.username)

    # Register bot commands — public (for all users)
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Начать работу с ботом"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="keywords", description="Мои ключевые слова"),
            BotCommand(command="add_keyword", description="Добавить ключевое слово"),
            BotCommand(command="remove_keyword", description="Удалить ключевое слово"),
            BotCommand(command="suggest_channel", description="Предложить канал"),
            BotCommand(command="more", description="Ещё пропущенные вакансии"),
            BotCommand(command="status", description="Мой статус"),
        ],
    ))

    # Register admin commands (visible only to admin)
    admin_entity = await bot.get_input_entity(ADMIN_ID)
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopePeer(peer=admin_entity),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Начать работу с ботом"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="keywords", description="Мои ключевые слова"),
            BotCommand(command="add_keyword", description="Добавить ключевое слово"),
            BotCommand(command="remove_keyword", description="Удалить ключевое слово"),
            BotCommand(command="more", description="Ещё пропущенные вакансии"),
            BotCommand(command="status", description="Мой статус"),
            BotCommand(command="channels", description="[ADMIN] Список каналов"),
            BotCommand(command="add_channel", description="[ADMIN] Добавить канал"),
            BotCommand(command="remove_channel", description="[ADMIN] Удалить канал"),
            BotCommand(command="users", description="[ADMIN] Список пользователей"),
            BotCommand(command="suggestions", description="[ADMIN] Предложения каналов"),
        ],
    ))
    log.info("Команды бота зарегистрированы.")

    # Catch-up missed messages
    await catch_up()

    # Start periodic save of last_check_time
    asyncio.get_event_loop().create_task(periodic_save())

    # Graceful shutdown: save last_check_time on exit
    def _shutdown_handler():
        db.save_last_check_time()
        log.info("last_check_time сохранён при остановке.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, _shutdown_handler)

    log.info("✅ Мониторинг запущен!")

    await user.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
