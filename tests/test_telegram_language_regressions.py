import json
from pathlib import Path
import tempfile
import unittest

from interaction_state import PersistentInteractionState
from language_detection import detect_language_evidence
from telegram_dispatcher import TelegramInteractionDispatcher


class TelegramLanguageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_short_english_replaces_provisional_spanish_on_first_reply(self):
        for text in ("Hi", "How much?"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                deliveries = []

                async def send_response(_peer, key, language, _fingerprint):
                    deliveries.append((key, language))

                dispatcher = TelegramInteractionDispatcher(
                    PersistentInteractionState(state_path), send_response
                )
                await dispatcher.dispatch(chat_id=1, event_id="image:1", kind="content", provisional_language="es")
                provisional = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
                self.assertTrue(provisional["language_provisional"])
                evidence = detect_language_evidence(text)
                self.assertEqual("en", evidence["language"])
                self.assertFalse(evidence["strong"])
                await dispatcher.dispatch(
                    chat_id=1, event_id="message:2", kind="content", language_evidence=evidence
                )

                self.assertEqual([("step1", "es"), ("step2", "en")], deliveries)
                contact = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
                self.assertEqual("en", contact["language"])
                self.assertTrue(contact["language_provisional"])

    async def test_real_weak_english_needs_two_delivered_observations_after_confirmed_spanish(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            deliveries = []

            async def send_response(_peer, key, language, _fingerprint):
                deliveries.append((key, language))

            dispatcher = TelegramInteractionDispatcher(PersistentInteractionState(state_path), send_response)
            for index, text in enumerate(("Hola, necesito ayuda", "Hi", "How much?")):
                await dispatcher.dispatch(
                    chat_id=1, event_id=f"message:{index}", kind="content",
                    language_evidence=detect_language_evidence(text),
                )

            self.assertEqual([("step1", "es"), ("step2", "es"), ("step2", "en")], deliveries)
            contact = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
            self.assertEqual("detected", contact["language_source"])

    async def test_failed_english_delivery_does_not_commit_language_or_duplicate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            attempts = []
            fail_english = True

            async def send_response(_peer, key, language, fingerprint):
                attempts.append((key, language, fingerprint))
                if language == "en" and fail_english:
                    raise RuntimeError("synthetic delivery failure")

            dispatcher = TelegramInteractionDispatcher(PersistentInteractionState(state_path), send_response)
            await dispatcher.dispatch(chat_id=1, event_id="image:1", kind="content", provisional_language="es")
            before_failure = state_path.read_bytes()
            english_request = {
                "chat_id": 1, "event_id": "message:2", "kind": "content",
                "language_evidence": detect_language_evidence("Hi"),
            }
            with self.assertRaisesRegex(RuntimeError, "synthetic delivery failure"):
                await dispatcher.dispatch(**english_request)
            self.assertEqual(before_failure, state_path.read_bytes())

            fail_english = False
            reloaded = TelegramInteractionDispatcher(PersistentInteractionState(state_path), send_response)
            retried = await reloaded.dispatch(**english_request)
            duplicate = await reloaded.dispatch(**english_request)
            self.assertEqual("en", retried.language)
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(3, len(attempts))
            self.assertEqual(attempts[1], attempts[2])
            contact = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
            self.assertTrue(contact["language_provisional"])
            self.assertEqual(1, contact["language_candidate_streak"])

    async def test_ambiguous_and_negated_english_preserve_provisional_spanish(self):
        for text in ("ok 👍", "I don't speak English"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                deliveries = []

                async def send_response(_peer, key, language, _fingerprint):
                    deliveries.append((key, language))

                dispatcher = TelegramInteractionDispatcher(
                    PersistentInteractionState(Path(directory) / "state.json"), send_response
                )
                await dispatcher.dispatch(chat_id=1, event_id="image:1", kind="content", provisional_language="es")
                decision = await dispatcher.dispatch(
                    chat_id=1, event_id="message:2", kind="content",
                    language_evidence=detect_language_evidence(text),
                )
                self.assertEqual("es", decision.language)
                self.assertEqual([("step1", "es"), ("step2", "es")], deliveries)

