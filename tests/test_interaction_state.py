import json
from pathlib import Path
import tempfile
import unittest

from interaction_state import PersistentInteractionState


class PersistentInteractionStateTests(unittest.TestCase):
    def test_preview_does_not_consume_interaction_before_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = PersistentInteractionState(state_path)

            preview = state.preview(
                contact_id="customer",
                event_id="call:1",
                kind="call",
            )

            self.assertEqual("call", preview.response_key)
            self.assertFalse(state_path.exists())
            committed = state.register(
                contact_id="customer",
                event_id="call:1",
                kind="call",
            )
            self.assertEqual("call", committed.response_key)

    def make_store(self, directory: str, **kwargs) -> PersistentInteractionState:
        return PersistentInteractionState(Path(directory) / "state.json", **kwargs)

    def test_every_distinct_call_is_call_and_later_content_is_step2(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)

            first = store.register(contact_id=1, event_id="call:1", kind="call")
            second = store.register(contact_id=1, event_id="call:2", kind="call")
            third = store.register(contact_id=1, event_id="message:3", kind="content")

            self.assertEqual("call", first.response_key)
            self.assertEqual("call", second.response_key)
            self.assertEqual("step2", third.response_key)
            self.assertEqual([1, 2, 2], [first.phase, second.phase, third.phase])

    def test_call_after_content_is_call_and_following_content_is_step2(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)

            first = store.register(contact_id=1, event_id="message:1", kind="content")
            call = store.register(contact_id=1, event_id="call:2", kind="call")
            following = store.register(
                contact_id=1,
                event_id="message:3",
                kind="content",
            )

            self.assertEqual("step1", first.response_key)
            self.assertEqual("call", call.response_key)
            self.assertEqual("step2", following.response_key)
            self.assertEqual([1, 2, 2], [first.phase, call.phase, following.phase])

    def test_first_content_is_step1_for_text_voice_image_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, label in enumerate(("text", "voice", "image", "document"), start=1):
                store = self.make_store(directory + label)
                decision = store.register(
                    contact_id=index,
                    event_id=f"message:{index}",
                    kind="content",
                    detected_language="es",
                )
                self.assertEqual("step1", decision.response_key)

    def test_duplicate_does_not_advance_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = store.register(contact_id=10, event_id="message:1", kind="content")
            duplicate = store.register(contact_id=10, event_id="message:1", kind="content")
            reloaded = self.make_store(directory)
            duplicate_after_restart = reloaded.register(
                contact_id=10,
                event_id="message:1",
                kind="content",
            )
            second = reloaded.register(contact_id=10, event_id="message:2", kind="content")

            self.assertEqual("step1", first.response_key)
            self.assertTrue(duplicate.duplicate)
            self.assertTrue(duplicate_after_restart.duplicate)
            self.assertEqual("step2", second.response_key)

    def test_contacts_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            one = store.register(contact_id="one", event_id="message:a", kind="content")
            two = store.register(contact_id="two", event_id="message:a", kind="content")
            one_again = store.register(contact_id="one", event_id="message:b", kind="content")

            self.assertEqual("step1", one.response_key)
            self.assertEqual("step1", two.response_key)
            self.assertEqual("step2", one_again.response_key)

    def test_language_can_be_detected_after_an_initial_non_text_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory, default_language="es")
            call = store.register(contact_id=1, event_id="call:1", kind="call")
            text = store.register(
                contact_id=1,
                event_id="message:2",
                kind="content",
                detected_language="fr",
            )

            self.assertEqual("es", call.language)
            self.assertEqual("fr", text.language)
            self.assertEqual("step2", text.response_key)

    def test_telegram_spanish_is_provisional_until_text_proves_language(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            store = self.make_store(directory, default_language="es")
            image = store.register(
                contact_id=1,
                event_id="message:image",
                kind="content",
                provisional_language="es",
            )
            english = store.register(
                contact_id=1,
                event_id="message:text",
                kind="content",
                detected_language="en",
                provisional_language="es",
            )
            later_french = store.register(
                contact_id=1,
                event_id="message:later",
                kind="content",
                detected_language="fr",
            )

            self.assertEqual("es", image.language)
            self.assertEqual("en", english.language)
            self.assertEqual("en", later_french.language)
            contact = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
            self.assertEqual("en", contact["language"])
            self.assertFalse(contact["language_provisional"])

    def test_initial_detected_text_is_confirmed_over_provisional_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            decision = store.register(
                contact_id=1,
                event_id="message:text",
                kind="content",
                detected_language="fr",
                provisional_language="es",
            )

            self.assertEqual("fr", decision.language)
            self.assertFalse(next(iter(store._contacts.values()))["language_provisional"])

    def test_state_file_does_not_contain_raw_customer_or_event_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.register(
                contact_id="573001234567@s.whatsapp.net",
                event_id="sensitive-event-id",
                kind="content",
            )
            serialized = (Path(directory) / "state.json").read_text(encoding="utf-8")

            self.assertNotIn("573001234567", serialized)
            self.assertNotIn("sensitive-event-id", serialized)
            self.assertEqual(1, json.loads(serialized)["version"])

    def test_valid_json_with_wrong_root_shape_does_not_crash_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text("[]", encoding="utf-8")

            store = self.make_store(directory)
            decision = store.register(contact_id=1, event_id="message:1", kind="content")

            self.assertEqual("step1", decision.response_key)


if __name__ == "__main__":
    unittest.main()
