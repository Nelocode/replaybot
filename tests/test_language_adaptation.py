import json
from pathlib import Path
import tempfile
import unittest

from interaction_state import PersistentInteractionState
from language_adaptation import reduce_language_state
from language_detection import detect_language_evidence


class LanguageAdaptationRegressionTests(unittest.TestCase):
    def test_pre_rollout_provisional_candidate_confirms_on_next_matching_observation(self):
        previous = {
            "language": "es",
            "language_source": "provisional",
            "language_provisional": True,
            "language_candidate": "en",
            "language_candidate_streak": 1,
        }
        result = reduce_language_state(
            previous, language_evidence=detect_language_evidence("How much?")
        )
        self.assertEqual("en", result["language"])
        self.assertEqual("detected", result["language_source"])
        self.assertFalse(result["language_provisional"])
        self.assertIsNone(result["language_candidate"])
        self.assertEqual(0, result["language_candidate_streak"])
        self.assertEqual("es", previous["language"])

    def test_pre_rollout_candidate_survives_store_reload_and_confirms_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            store = PersistentInteractionState(state_path)
            store.register(contact_id=1, event_id="image:1", kind="content", provisional_language="es")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            contact = next(iter(saved["contacts"].values()))
            # Old policy retained provisional ES after its first weak EN text.
            contact["language_candidate"] = "en"
            contact["language_candidate_streak"] = 1
            state_path.write_text(json.dumps(saved), encoding="utf-8")

            reloaded = PersistentInteractionState(state_path)
            decision = reloaded.register(
                contact_id=1, event_id="message:next", kind="content",
                language_evidence=detect_language_evidence("How much?"),
            )
            self.assertEqual("en", decision.language)
            contact = next(iter(json.loads(state_path.read_text(encoding="utf-8"))["contacts"].values()))
            self.assertEqual("detected", contact["language_source"])
            self.assertFalse(contact["language_provisional"])
            self.assertIsNone(contact["language_candidate"])
            self.assertEqual(0, contact["language_candidate_streak"])

    def test_legacy_and_operator_seed_still_require_two_weak_observations(self):
        for source in ("legacy", "operator_seed", "detected"):
            with self.subTest(source=source):
                state = {
                    "language": "es",
                    "language_source": source,
                    "language_provisional": False,
                }
                first = reduce_language_state(state, language_evidence=detect_language_evidence("Hi"))
                self.assertEqual("es", first["language"])
                self.assertEqual(source, first["language_source"])
                self.assertEqual("en", first["language_candidate"])
                second = reduce_language_state(first, language_evidence=detect_language_evidence("How much?"))
                self.assertEqual("en", second["language"])
                self.assertEqual("detected", second["language_source"])
                self.assertIsNone(second["language_candidate"])

    def test_unannotated_persisted_language_is_legacy_not_provisional(self):
        result = reduce_language_state(
            {"language": "es"}, language_evidence=detect_language_evidence("Hi")
        )
        self.assertEqual("es", result["language"])
        self.assertEqual("legacy", result["language_source"])
        self.assertFalse(result["language_provisional"])

    def test_first_weak_text_can_be_replaced_without_premature_confirmation(self):
        for initial, incoming in (("es", "en"), ("en", "fr"), ("fr", "es")):
            with self.subTest(initial=initial, incoming=incoming):
                def weak(language):
                    return {"language": language, "strong": False, "explicit": False, "score": 4, "margin": 4}

                original = reduce_language_state(None, language_evidence=weak(initial))
                changed = reduce_language_state(original, language_evidence=weak(incoming))
                self.assertEqual(incoming, changed["language"])
                self.assertTrue(changed["language_provisional"])
                self.assertEqual(incoming, changed["language_candidate"])
                self.assertEqual(1, changed["language_candidate_streak"])
                self.assertEqual(initial, original["language"])

                confirmed = reduce_language_state(changed, language_evidence=weak(incoming))
                self.assertEqual(incoming, confirmed["language"])
                self.assertFalse(confirmed["language_provisional"])


if __name__ == "__main__":
    unittest.main()
