import unittest

from language_detection import detect_supported_language


class LanguageDetectionTests(unittest.TestCase):
    def test_requires_a_unique_top_score(self):
        self.assertIsNone(detect_supported_language("photo"))
        self.assertIsNone(detect_supported_language("video"))
        self.assertIsNone(detect_supported_language("ok"))
        self.assertEqual("en", detect_supported_language("Are you available now?"))
        self.assertEqual("fr", detect_supported_language("bonjour"))

    def test_explicit_marker_breaks_ambiguity_unless_markers_tie(self):
        self.assertEqual("en", detect_supported_language("english photo"))
        self.assertIsNone(detect_supported_language("english français"))


if __name__ == "__main__":
    unittest.main()
