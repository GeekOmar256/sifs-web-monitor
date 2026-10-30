"""Tests for automatic image analysis without a live OpenAI request."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_server
from auto_analysis import analyze_photo


ASSESSMENT = {
    "crop_type": "Tomato",
    "growth_stage": "Seedling",
    "health_status": "No visible issue",
    "visible_issues": [],
    "recommendations": ["Continue monitoring"],
    "confidence": "medium",
}


class AutoAnalysisTests(unittest.TestCase):
    def test_openai_request_keeps_key_in_authorization_header(self):
        def fake_open(request, timeout):
            self.assertEqual(timeout, 45)
            self.assertEqual(request.get_header("Authorization"), "Bearer private-test-key")
            payload = json.loads(request.data)
            self.assertFalse(payload["store"])
            self.assertEqual(payload["input"][0]["content"][1]["image_url"],
                             "data:image/jpeg;base64,abc123")
            return io.BytesIO(json.dumps({"output": [{"content": [
                {"type": "output_text", "text": json.dumps(ASSESSMENT)}]}]}).encode())

        self.assertEqual(analyze_photo("abc123", "private-test-key", opener=fake_open), ASSESSMENT)

    def test_photos_are_analyzed_in_order_and_latest_result_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(local_server, "DB_FILE", path / "history.sqlite3"), \
                 patch.object(local_server, "SETTINGS_FILE", path / "settings.json"):
                state = local_server.LocalState()
                state.set_photo("old-image", "2026-10-30T08:00:00+00:00")
                old_id = state.next_analysis_job()[0]
                state.set_photo("new-image", "2026-10-30T08:05:00+00:00")
                self.assertEqual(state.next_analysis_job()[0], old_id)
                self.assertTrue(state.complete_analysis(old_id,
                    {**ASSESSMENT, "photoCapturedAt": "2026-10-30T08:00:00+00:00"}))
                photo_id, image, captured_at = state.next_analysis_job()
                self.assertNotEqual(old_id, photo_id)
                self.assertEqual(image, "new-image")
                self.assertTrue(state.complete_analysis(photo_id,
                    {**ASSESSMENT, "photoCapturedAt": captured_at}))
                self.assertIsNone(state.next_analysis_job())
                self.assertEqual(state.snapshot()["analysisStatus"], "complete")
                self.assertEqual(len(state.history("analyses")), 2)
                restarted = local_server.LocalState()
                self.assertEqual(restarted.snapshot()["photo"]["capturedAt"], captured_at)
                self.assertEqual(restarted.snapshot()["analysis"]["photoCapturedAt"], captured_at)

    def test_failure_is_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(local_server, "DB_FILE", path / "history.sqlite3"), \
                 patch.object(local_server, "SETTINGS_FILE", path / "settings.json"):
                state = local_server.LocalState()
                state.set_photo("image", "2026-10-30T08:00:00+00:00")
                photo_id = state.next_analysis_job()[0]
                self.assertEqual(state.defer_analysis(photo_id, "temporary error"), 10)
                self.assertIsNone(state.next_analysis_job())
                self.assertEqual(state.snapshot()["analysisStatus"], "retrying")


if __name__ == "__main__":
    unittest.main()
