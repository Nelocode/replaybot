from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("telegram", SimpleNamespace(Update=object))
sys.modules.setdefault(
    "telegram.ext",
    SimpleNamespace(
        Application=object,
        CommandHandler=object,
        MessageHandler=object,
        filters=SimpleNamespace(),
        ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),
    ),
)

from language_detection import detect_language_evidence
import botfather_bot


class BotFatherHandlerLanguageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        botfather_bot.user_state.clear()
        self.messages = {
            language: {
                "steps": [
                    (f"{language}-step1", "", False),
                    (f"{language}-step2", "", False),
                ],
                "call": {"text": f"{language}-call", "audio": ""},
            }
            for language in ("es", "en", "fr")
        }

    @staticmethod
    def update(*, text=None, caption=None, chat_id=7, voice=None, video_note=None):
        message = SimpleNamespace(
            text=text,
            caption=caption,
            voice=voice,
            video_note=video_note,
            reply_text=AsyncMock(),
            reply_audio=AsyncMock(),
        )
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            message=message,
        )

    async def test_real_short_english_replaces_no_text_spanish_on_first_reply(self):
        for chat_id, text in enumerate(("Hi", "How much?"), start=101):
            with self.subTest(text=text):
                image = self.update(chat_id=chat_id)
                english = self.update(chat_id=chat_id, text=text)
                evidence = detect_language_evidence(text)
                self.assertEqual("en", evidence["language"])
                self.assertFalse(evidence["strong"])
                with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
                    botfather_bot, "load_messages_fresh"
                ):
                    await botfather_bot.handle_message(image, None)
                    self.assertTrue(botfather_bot.user_state[chat_id]["language_provisional"])
                    await botfather_bot.handle_message(english, None)

                image.message.reply_text.assert_awaited_once_with("es-step1")
                english.message.reply_text.assert_awaited_once_with("en-step2")
                self.assertEqual("en", botfather_bot.user_state[chat_id]["lang"])

    async def test_real_weak_english_still_needs_two_observations_after_confirmed_spanish(self):
        spanish = self.update(text="Hola, necesito ayuda", chat_id=103)
        first = self.update(text="Hi", chat_id=103)
        second = self.update(text="How much?", chat_id=103)
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot, "load_messages_fresh"
        ):
            await botfather_bot.handle_message(spanish, None)
            self.assertFalse(botfather_bot.user_state[103]["language_provisional"])
            await botfather_bot.handle_message(first, None)
            first.message.reply_text.assert_awaited_once_with("es-step2")
            await botfather_bot.handle_message(second, None)

        second.message.reply_text.assert_awaited_once_with("en-step2")
        self.assertFalse(botfather_bot.user_state[103]["language_provisional"])

    async def test_ambiguous_and_negated_english_leave_provisional_spanish_unchanged(self):
        for chat_id, text in enumerate(("ok 👍", "I don't speak English"), start=104):
            with self.subTest(text=text):
                image = self.update(chat_id=chat_id)
                followup = self.update(chat_id=chat_id, text=text)
                with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
                    botfather_bot, "load_messages_fresh"
                ):
                    await botfather_bot.handle_message(image, None)
                    await botfather_bot.handle_message(followup, None)

                followup.message.reply_text.assert_awaited_once_with("es-step2")
                self.assertTrue(botfather_bot.user_state[chat_id]["language_provisional"])

    async def test_explicit_english_request_changes_confirmed_spanish_immediately(self):
        spanish = self.update(text="Hola, necesito ayuda", chat_id=106)
        english = self.update(text="English please", chat_id=106)
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot, "load_messages_fresh"
        ):
            await botfather_bot.handle_message(spanish, None)
            await botfather_bot.handle_message(english, None)

        english.message.reply_text.assert_awaited_once_with("en-step2")
        self.assertFalse(botfather_bot.user_state[106]["language_provisional"])

