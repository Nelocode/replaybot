from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch


# The production dependency is installed by the panel when this optional
# worker is started. Unit tests exercise the handlers without requiring a
# token, network access, or the Telegram package itself.
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

import botfather_bot


class BotFatherHandlerTests(unittest.IsolatedAsyncioTestCase):
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
    def update(
        *, text=None, caption=None, chat_id=7, voice=None, video_note=None,
        message_id=None,
    ):
        message = SimpleNamespace(
            message_id=message_id,
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

    async def test_tied_image_caption_stays_provisional_then_clear_text_wins(self):
        image = self.update(caption="photo")
        english = self.update(text="Are you available now?", chat_id=7)
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot,
            "load_messages_fresh",
        ):
            await botfather_bot.handle_message(image, None)
            await botfather_bot.handle_message(english, None)

        image.message.reply_text.assert_awaited_once_with("es-step1")
        english.message.reply_text.assert_awaited_once_with("en-step2")
        self.assertEqual("en", botfather_bot.user_state[7]["lang"])
        self.assertFalse(botfather_bot.user_state[7]["language_provisional"])

    async def test_voice_with_english_caption_uses_confirmed_english_call(self):
        voice = self.update(
            caption="Are you available now?",
            chat_id=8,
            voice=object(),
        )
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot,
            "load_messages_fresh",
        ):
            await botfather_bot.handle_call(voice, None)

        voice.message.reply_text.assert_awaited_once_with("en-call")
        self.assertEqual("en", botfather_bot.user_state[8]["lang"])
        self.assertFalse(botfather_bot.user_state[8]["language_provisional"])

    async def test_video_note_without_caption_uses_provisional_spanish_call(self):
        video_note = self.update(chat_id=9, video_note=object())
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot,
            "load_messages_fresh",
        ):
            await botfather_bot.handle_call(video_note, None)

        video_note.message.reply_text.assert_awaited_once_with("es-call")
        self.assertEqual("es", botfather_bot.user_state[9]["lang"])
        self.assertTrue(botfather_bot.user_state[9]["language_provisional"])

    async def test_strong_spanish_text_overrides_confirmed_french(self):
        french = self.update(text="bonjour", chat_id=10)
        spanish = self.update(
            text="que chicas están disponibles por Rubí",
            chat_id=10,
        )
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot,
            "load_messages_fresh",
        ):
            await botfather_bot.handle_message(french, None)
            await botfather_bot.handle_message(spanish, None)

        french.message.reply_text.assert_awaited_once_with("fr-step1")
        spanish.message.reply_text.assert_awaited_once_with("es-step2")
        state = botfather_bot.user_state[10]
        self.assertEqual("es", state["lang"])
        self.assertEqual("detected", state["language_source"])
        self.assertIsNone(state["language_candidate"])

    async def test_duplicate_weak_message_does_not_confirm_candidate_or_advance_step(self):
        french = self.update(text="bonjour", chat_id=11, message_id=1)
        weak = self.update(text="want", chat_id=11, message_id=2)
        duplicate = self.update(text="want", chat_id=11, message_id=2)
        with patch.object(botfather_bot, "MESSAGES", self.messages), patch.object(
            botfather_bot,
            "load_messages_fresh",
        ):
            await botfather_bot.handle_message(french, None)
            await botfather_bot.handle_message(weak, None)
            await botfather_bot.handle_message(duplicate, None)

        state = botfather_bot.user_state[11]
        self.assertEqual("fr", state["lang"])
        self.assertEqual("en", state["language_candidate"])
        self.assertEqual(1, state["language_candidate_streak"])
        self.assertEqual(1, state["step"])
        duplicate.message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
