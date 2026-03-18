import os
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID"))

CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Config helpers ──────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ── Clients ─────────────────────────────────────────────────────

# Userbot client — слушает каналы от вашего имени
user = TelegramClient("user_session", API_ID, API_HASH)

# Bot client — отправляет вам уведомления
bot = TelegramClient("bot_session", API_ID, API_HASH)


# ── Channel monitor (userbot) ──────────────────────────────────

@user.on(events.NewMessage)
async def on_channel_message(event):
    """Проверяет новые сообщения в отслеживаемых каналах."""
    chat = await event.get_chat()
    chat_username = getattr(chat, "username", None)
    chat_id = event.chat_id

    cfg = load_config()
    channels = cfg.get("channels", [])
    keywords = cfg.get("keywords", [])

    if not channels or not keywords:
        return

    # Проверяем, отслеживается ли канал (по username или id)
    is_tracked = False
    for ch in channels:
        ch_str = str(ch).lower().lstrip("@")
        if chat_username and chat_username.lower() == ch_str:
            is_tracked = True
            break
        if str(chat_id) == ch_str:
            is_tracked = True
            break

    if not is_tracked:
        return

    log.info("📩 Сообщение из отслеживаемого канала: %s (id=%s)", chat_username, chat_id)

    text = (event.raw_text or "").lower()
    if not text:
        return

    matched = [kw for kw in keywords if kw.lower() in text]
    if not matched:
        log.info("  Нет совпадений по ключевым словам.")
        return

    # Формируем уведомление
    channel_name = chat.title if hasattr(chat, "title") else str(chat_id)
    link = f"https://t.me/{chat_username}/{event.id}" if chat_username else ""
    header = (
        f"🔔 **Найдено совпадение!**\n"
        f"📢 Канал: **{channel_name}**\n"
        f"🔑 Слова: {', '.join(matched)}\n"
    )
    if link:
        header += f"🔗 [Открыть сообщение]({link})\n"
    header += f"{'─' * 30}"

    try:
        await bot.send_message(MY_USER_ID, header, link_preview=False)
        # Пересылаем оригинал через userbot
        await user.forward_messages(MY_USER_ID, event.id, chat_id)
        log.info("Переслано из %s, слова: %s", channel_name, matched)
    except Exception as e:
        log.error("Ошибка отправки: %s", e)


# ── Bot commands (управление через бота) ───────────────────────

@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if event.sender_id != MY_USER_ID:
        return
    await event.respond(
        "👋 **Telegram Channel Monitor**\n\n"
        "Команды:\n"
        "/channels — список каналов\n"
        "/add_channel `@username` — добавить канал\n"
        "/remove_channel `@username` — удалить канал\n"
        "/keywords — список ключевых слов\n"
        "/add_keyword `слово` — добавить слово\n"
        "/remove_keyword `слово` — удалить слово\n"
        "/status — текущий статус"
    )


@bot.on(events.NewMessage(pattern="/channels"))
async def cmd_channels(event):
    if event.sender_id != MY_USER_ID:
        return
    cfg = load_config()
    channels = cfg.get("channels", [])
    if not channels:
        await event.respond("📢 Список каналов пуст. Добавьте: /add_channel @username")
        return
    lines = "\n".join(f"  • {ch}" for ch in channels)
    await event.respond(f"📢 **Отслеживаемые каналы:**\n{lines}")


@bot.on(events.NewMessage(pattern=r"/add_channel\s+(.+)"))
async def cmd_add_channel(event):
    if event.sender_id != MY_USER_ID:
        return
    channel = event.pattern_match.group(1).strip().lstrip("@")
    cfg = load_config()
    if channel.lower() in [c.lower() for c in cfg["channels"]]:
        await event.respond(f"⚠️ Канал `{channel}` уже в списке.")
        return
    cfg["channels"].append(channel)
    save_config(cfg)
    await event.respond(f"✅ Канал `@{channel}` добавлен!")


@bot.on(events.NewMessage(pattern=r"/remove_channel\s+(.+)"))
async def cmd_remove_channel(event):
    if event.sender_id != MY_USER_ID:
        return
    channel = event.pattern_match.group(1).strip().lstrip("@")
    cfg = load_config()
    filtered = [c for c in cfg["channels"] if c.lower() != channel.lower()]
    if len(filtered) == len(cfg["channels"]):
        await event.respond(f"⚠️ Канал `{channel}` не найден.")
        return
    cfg["channels"] = filtered
    save_config(cfg)
    await event.respond(f"🗑 Канал `@{channel}` удалён.")


@bot.on(events.NewMessage(pattern="/keywords$"))
async def cmd_keywords(event):
    if event.sender_id != MY_USER_ID:
        return
    cfg = load_config()
    keywords = cfg.get("keywords", [])
    if not keywords:
        await event.respond("🔑 Список слов пуст. Добавьте: /add_keyword слово")
        return
    lines = "\n".join(f"  • {kw}" for kw in keywords)
    await event.respond(f"🔑 **Ключевые слова:**\n{lines}")


@bot.on(events.NewMessage(pattern=r"/add_keyword\s+(.+)"))
async def cmd_add_keyword(event):
    if event.sender_id != MY_USER_ID:
        return
    keyword = event.pattern_match.group(1).strip()
    cfg = load_config()
    if keyword.lower() in [k.lower() for k in cfg["keywords"]]:
        await event.respond(f"⚠️ Слово `{keyword}` уже в списке.")
        return
    cfg["keywords"].append(keyword)
    save_config(cfg)
    await event.respond(f"✅ Слово `{keyword}` добавлено!")


@bot.on(events.NewMessage(pattern=r"/remove_keyword\s+(.+)"))
async def cmd_remove_keyword(event):
    if event.sender_id != MY_USER_ID:
        return
    keyword = event.pattern_match.group(1).strip()
    cfg = load_config()
    filtered = [k for k in cfg["keywords"] if k.lower() != keyword.lower()]
    if len(filtered) == len(cfg["keywords"]):
        await event.respond(f"⚠️ Слово `{keyword}` не найдено.")
        return
    cfg["keywords"] = filtered
    save_config(cfg)
    await event.respond(f"🗑 Слово `{keyword}` удалено.")


@bot.on(events.NewMessage(pattern="/status"))
async def cmd_status(event):
    if event.sender_id != MY_USER_ID:
        return
    cfg = load_config()
    ch_count = len(cfg.get("channels", []))
    kw_count = len(cfg.get("keywords", []))
    await event.respond(
        f"📊 **Статус**\n"
        f"Каналов: {ch_count}\n"
        f"Ключевых слов: {kw_count}\n"
        f"Мониторинг: {'✅ активен' if ch_count and kw_count else '⏸ нужны каналы и слова'}"
    )


# ── Main ────────────────────────────────────────────────────────

async def main():
    log.info("Запуск userbot...")
    await user.start()
    log.info("Userbot подключён: %s", (await user.get_me()).first_name)

    log.info("Запуск бота...")
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Бот подключён: @%s", bot_me.username)

    # Регистрируем команды бота, чтобы Telegram показывал подсказки при вводе "/"
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Запуск бота и справка"),
            BotCommand(command="channels", description="Список отслеживаемых каналов"),
            BotCommand(command="add_channel", description="Добавить канал (@username)"),
            BotCommand(command="remove_channel", description="Удалить канал (@username)"),
            BotCommand(command="keywords", description="Список ключевых слов"),
            BotCommand(command="add_keyword", description="Добавить ключевое слово"),
            BotCommand(command="remove_keyword", description="Удалить ключевое слово"),
            BotCommand(command="status", description="Текущий статус мониторинга"),
        ],
    ))
    log.info("📋 Команды бота зарегистрированы.")

    log.info("✅ Мониторинг запущен! Управляйте через бота.")

    await user.run_until_disconnected()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
