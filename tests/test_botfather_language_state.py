import unittest
from types import SimpleNamespace

from botfather_language_state import (
    apply_language_evidence,
    apply_message_language_evidence,
    detect_supported_language,
)


class BotFatherLanguageStateTests(unittest.TestCase):
    def test_detection_accepts_english_evidence_but_not_ambiguous_ok(self):
        self.assertEqual("en", detect_supported_language("hello"))
        self.assertEqual("en", detect_supported_language("Are you available now"))
        self.assertIsNone(detect_supported_language("ok"))

    def test_non_text_starts_spanish_provisional_then_text_replaces_it(self):
        state = {}
        self.assertEqual("es", apply_language_evidence(state, detected_language=None))
        self.assertTrue(state["language_provisional"])
        self.assertEqual("en", apply_language_evidence(state, detected_language="en"))
        self.assertFalse(state["language_provisional"])

    def test_initial_text_is_confirmed_and_does_not_keep_flipping(self):
        state = {}
        self.assertEqual("fr", apply_language_evidence(state, detected_language="fr"))
        self.assertFalse(state["language_provisional"])
        self.assertEqual("fr", apply_language_evidence(state, detected_language="en"))

    def test_legacy_language_without_flag_is_confirmed(self):
        state = {"lang": "es"}
        self.assertEqual("es", apply_language_evidence(state, detected_language="en"))
        self.assertFalse(state["language_provisional"])

    def test_voice_with_english_caption_confirms_english_for_call_response(self):
        state = {}
        voice = SimpleNamespace(
            text=None,
            caption="Are you available now?",
            voice=object(),
            video_note=None,
        )

        self.assertEqual("en", apply_message_language_evidence(state, voice))
        self.assertFalse(state["language_provisional"])

    def test_video_note_without_caption_uses_provisional_spanish(self):
        state = {}
        video_note = SimpleNamespace(
            text=None,
            caption=None,
            voice=None,
            video_note=object(),
        )

        self.assertEqual("es", apply_message_language_evidence(state, video_note))
        self.assertTrue(state["language_provisional"])

    def test_tied_media_caption_stays_provisional_then_clear_text_replaces_it(self):
        state = {}
        tied_photo = SimpleNamespace(text=None, caption="photo")
        english = SimpleNamespace(text="Are you available now?", caption=None)

        self.assertEqual("es", apply_message_language_evidence(state, tied_photo))
        self.assertTrue(state["language_provisional"])
        self.assertEqual("en", apply_message_language_evidence(state, english))
        self.assertFalse(state["language_provisional"])


if __name__ == "__main__":
    unittest.main()
