from __future__ import annotations

import unittest

from pipeline_google_omni_stack.publish_finish import build_captions_from_words


class PublishFinishTests(unittest.TestCase):
    def test_captions_follow_final_audio_word_timestamps(self):
        words = [
            {"word": "Entró", "start": 0.62, "end": 0.91},
            {"word": "Ana", "start": 0.94, "end": 1.18},
            {"word": "García.", "start": 1.21, "end": 1.62},
            {"word": "Hola", "start": 2.20, "end": 2.45},
            {"word": "Ana.", "start": 2.48, "end": 2.81},
        ]

        captions = build_captions_from_words(words, final_duration=3.0)

        self.assertEqual([item["text"] for item in captions], ["Entró Ana García.", "Hola Ana."])
        self.assertEqual(captions[0]["start"], 0.62)
        self.assertEqual(captions[1]["start"], 2.2)
        self.assertNotIn("scene", " ".join(item["text"] for item in captions).lower())

    def test_caption_does_not_start_before_first_spoken_word(self):
        captions = build_captions_from_words(
            [{"word": "Sistema", "start": 1.37, "end": 1.8}],
            final_duration=2.0,
        )

        self.assertEqual(captions[0]["start"], 1.37)

    def test_adjacent_captions_never_overlap(self):
        captions = build_captions_from_words(
            [
                {"word": "pagó", "start": 1.66, "end": 1.70},
                {"word": "ochenta", "start": 1.70, "end": 2.10},
                {"word": "mil.", "start": 2.10, "end": 2.40},
                {"word": "Entró", "start": 2.38, "end": 2.70},
                {"word": "Ana.", "start": 2.70, "end": 3.00},
            ],
            final_duration=3.1,
        )

        self.assertLessEqual(captions[0]["end"], captions[1]["start"])


if __name__ == "__main__":
    unittest.main()
