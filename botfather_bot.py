"""
BotFather Bot — Telegram (Bot API)
Usa python-telegram-bot. Misma lógica de idioma y pasos que el user bot.
Requiere AUTOREPLY_BOT_TOKEN. Corre en paralelo con bot.py (Telethon).
"""
import os
import re
import json
import time
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audios"
MESSAGES_FILE = DATA_DIR / "messages.json"
RESET_TIMEOUT = 3600

BOT_TOKEN = os.environ.get("AUTOREPLY_BOT_TOKEN")

# ── Intentar cargar de .env.local si no está en environment ────────
def _load_env_file():
    env_file = DATA_DIR / ".env.local"
    if not env_file.exists():
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "AUTOREPLY_BOT_TOKEN" and val:
                os.environ[key] = val

_load_env_file()
BOT_TOKEN = os.environ.get("AUTOREPLY_BOT_TOKEN")  # Re-leer

if not BOT_TOKEN:
    logging.warning("AUTOREPLY_BOT_TOKEN not set. BotFather bot will not start.")
    # Don't crash — just log and exit gracefully
    import sys
    sys.exit(0)

# ── Mensajes ───────────────────────────────────────────────────────────
def load_messages() -> dict:
    with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for lang, lang_data in data.items():
        steps = lang_data.get("steps", [])
        result[lang] = {
            "steps": [(s["text"], s["audio"], s.get("loop", False)) for s in steps],
            "call": lang_data.get("call", {"text": "📞 Llamada recibida", "audio": ""})
        }
    return result

MESSAGES = load_messages()

# ── Estado por usuario ────────────────────────────────────────────────
user_state: dict[int, dict] = {}

# ── Detección de idioma ───────────────────────────────────────────────
LANG_KEYWORDS = {
    "es": re.compile(
        r"\b(hola|gracias|por\s*favor|buenos\s*días|quiero|necesito|ayuda|habla|"
        r"buenas|amigo|claro|vale|dale|listo|entiendo|puedes|hacer|"
        r"dónde|cuándo|cómo|cuál|quién|eso|esto|algo|nada|todo|más|menos|"
        r"está|estoy|estamos|están|tengo|tiene|tenemos|soy|eres|somos|son)\b",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"\b(hello|hi|thanks|thank\s*you|please|help|want|need|can\s*i|"
        r"yes|sure|fine|good|great|hey|would|could|should|"
        r"where|when|how|what|who|that|this|there|here|"
        r"is|are|am|have|has|do|does|did|will|may|might)\b",
        re.IGNORECASE,
    ),
    "fr": re.compile(
        r"\b(bonjour|merci|s'il\s*vous\s*plaît|aide|besoin|vouloir|"
        r"oui|d'accord|bien|tres|peux|peut|"
        r"où|quand|comment|quoi|qui|que|"
        r"est|suis|sommes|êtes|sont|ai|as|a|avons|avez|ont|"
        r"je|tu|il|elle|nous|vous|ils|elles|"
        r"ce|cet|cette|ces|mon|ton|son|ma|ta|sa)\b",
        re.IGNORECASE,
    ),
}

AMBIGUOUS = {"ok", "no", "si", "hey", "hi", "hello"}


def detect_lang(text: str) -> str:
    scores = {"es": 0, "en": 0, "fr": 0}
    for lang, pattern in LANG_KEYWORDS.items():
        matches = pattern.findall(text)
        for m in matches:
            if m.lower() not in AMBIGUOUS:
                scores[lang] += 1.0
    lang_markers = {
        "es": re.compile(r"\b(español|castellano|hablo español|hablo espanol)\b", re.IGNORECASE),
        "en": re.compile(r"\b(english|speak english)\b", re.IGNORECASE),
        "fr": re.compile(r"\b(français|francais|parle français|parle francais)\b", re.IGNORECASE),
    }
    for lang, marker in lang_markers.items():
        if marker.search(text):
            scores[lang] += 20
    if max(scores.values()) < 1:
        return "en"
    return max(scores, key=scores.get)


def is_expired(state: dict) -> bool:
    return time.time() - state.get("last_seen", 0) > RESET_TIMEOUT


def load_messages_fresh():
    global MESSAGES
    try:
        MESSAGES = load_messages()
    except Exception:
        pass


# ── Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silent start — no welcome, user's first message triggers lang detection."""
    chat_id = update.effective_chat.id
    if chat_id in user_state:
        del user_state[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return

    now = time.time()
    state = user_state.get(chat_id)

    if state is None or is_expired(state):
        if state is not None:
            logging.info("[BF chat=%s] EXPIRED — new cycle", chat_id)
        load_messages_fresh()
        detected = detect_lang(text)
        state = {"lang": detected, "step": 0, "last_seen": now}
        user_state[chat_id] = state
        step_to_use = 0
    else:
        step_to_use = min(state["step"] + 1, len(MESSAGES.get(state["lang"], MESSAGES["en"])["steps"]) - 1)
        state["last_seen"] = now

    lang = state["lang"]
    lang_data = MESSAGES.get(lang, MESSAGES["en"])
    msg_text, audio_file, is_loop = lang_data["steps"][step_to_use]

    if not is_loop:
        state["step"] = step_to_use

    await update.message.reply_text(msg_text)

    audio_path = AUDIO_DIR / audio_file
    if audio_path.exists():
        with open(audio_path, "rb") as f:
            await update.message.reply_audio(
                audio=f,
                title=f"AutoReply ({lang.upper()})",
                performer="AutoReply BotFather",
            )

    logging.info(
        "[BF chat=%s lang=%s step=%s] %r → %r",
        chat_id, lang, step_to_use, text[:60], msg_text[:60],
    )


async def handle_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice/video note treated as call."""
    chat_id = update.effective_chat.id
    logging.info("[BF chat=%s] VOICE/VIDEO received", chat_id)

    state = user_state.get(chat_id)
    lang = state["lang"] if (state and not is_expired(state)) else "en"

    lang_data = MESSAGES.get(lang, MESSAGES["en"])
    call_data = lang_data.get("call", {"text": "📞 Llamada recibida", "audio": ""})
    msg_text = call_data.get("text", "📞 Llamada recibida")
    audio_file = call_data.get("audio", "")

    try:
        await update.message.reply_text(msg_text)
    except Exception:
        pass

    if audio_file:
        audio_path = AUDIO_DIR / audio_file
        if audio_path.exists():
            with open(audio_path, "rb") as f:
                try:
                    await update.message.reply_audio(
                        audio=f,
                        title=f"AutoReply ({lang.upper()}) - Call",
                        performer="AutoReply BotFather",
                    )
                except Exception:
                    pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("[BF] Error: %s", context.error)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        format="%(asctime)s [BF %(levelname)s] %(message)s",
        level=logging.INFO,
    )

    if not BOT_TOKEN:
        logging.warning("No AUTOREPLY_BOT_TOKEN — BotFather bot exiting.")
        return

    while True:
        try:
            app = Application.builder().token(BOT_TOKEN).build()

            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_call))
            app.add_error_handler(error_handler)

            logging.info("BotFather bot starting... (token=%s...)", BOT_TOKEN[:8])
            app.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logging.error("[BF] BotFather bot crashed: %s. Restarting in 5s...", e)
            time.sleep(5)


if __name__ == "__main__":
    main()

