"""Tests for YouTube Link Extraction Resilience, Diagnostics, Error Mapping, and Updater."""
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import config, media, server, auth, db


class TestYouTubeResilience(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pk_test_yt_")
        self.orig_data = config.DATA_DIR
        self.orig_cookies = config.YT_COOKIES
        self.orig_browser_file = config.YT_BROWSER_FILE

        config.DATA_DIR = Path(self.temp_dir)
        config.YT_COOKIES = config.DATA_DIR / "youtube-cookies.txt"
        config.YT_BROWSER_FILE = config.DATA_DIR / "youtube-browser.txt"
        db.init()

    def tearDown(self):
        config.DATA_DIR = self.orig_data
        config.YT_COOKIES = self.orig_cookies
        config.YT_BROWSER_FILE = self.orig_browser_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_js_runtimes(self):
        runtimes = media.find_js_runtimes()
        info = media.find_js_runtimes_info()
        self.assertIn("node", runtimes)
        self.assertIn("deno", runtimes)
        # Node should be detected on this Windows system
        self.assertTrue(info["node"]["available"])
        self.assertEqual(info["active"], "node")
        self.assertIn("NODE.EXE", runtimes["node"]["path"].upper())

    def test_friendly_error_mapping(self):
        # JS runtime error
        e1 = Exception("WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default")
        msg1 = media.friendly_error(e1)
        self.assertIn("JavaScript runtime", msg1)

        # Bot check / Sign in
        e2 = Exception("ERROR: [youtube] Ra6sNe8s2Hc: Sign in to confirm you’re not a bot. Use --cookies-from-browser")
        msg2 = media.friendly_error(e2)
        self.assertIn("bot check", msg2.lower())
        self.assertIn("cookies", msg2.lower())

        # Private video
        e3 = Exception("ERROR: [youtube] Private video. Sign in if you've been granted access to this video")
        msg3 = media.friendly_error(e3)
        self.assertIn("private", msg3.lower())

        # Members-only
        e4 = Exception("ERROR: [youtube] Join this channel to get access to members-only content")
        msg4 = media.friendly_error(e4)
        self.assertIn("members-only", msg4.lower())

        # Format / signature issue
        e5 = Exception("ERROR: [youtube] Requested format is not available. Use --list-formats for a list of available formats")
        msg5 = media.friendly_error(e5)
        self.assertIn("Update yt-dlp", msg5)

        # Geo-blocked
        e6 = Exception("ERROR: [youtube] The uploader has not made this video available in your country")
        msg6 = media.friendly_error(e6)
        self.assertIn("geo-blocked", msg6.lower())

    def test_cookies_diagnostic_states(self):
        # Not configured
        d1 = media.cookies_diagnostic()
        self.assertEqual(d1["status"], "not_set")

        # Browser configured
        config.set_yt_browser("edge")
        self.assertEqual(config.get_yt_browser(), "edge")
        d2 = media.cookies_diagnostic()
        self.assertEqual(d2["status"], "browser")
        self.assertEqual(d2["browser"], "edge")

        # Cookie file saved
        cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\txxx\n"
        config.YT_COOKIES.write_text(cookie_text, encoding="utf-8")
        d3 = media.cookies_diagnostic()
        self.assertEqual(d3["status"], "loaded")
        self.assertEqual(d3["count"], 1)

    def test_youtube_api_endpoints(self):
        base = "http://127.0.0.1:8765"
        origin = {"Origin": base}
        client = TestClient(server.app, base_url=base)

        # Diagnostics endpoint
        res = client.get("/api/youtube/diagnostics", headers=origin)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("ytdlp_version", data)
        self.assertIn("js_runtimes", data)
        self.assertIn("cookies", data)
        self.assertTrue(data["js_runtimes"]["node"]["available"])

        # Test Link endpoint with invalid URL
        r_bad = client.post("/api/youtube/test", json={"url": "https://not-youtube.com/watch?v=123"}, headers=origin)
        self.assertEqual(r_bad.status_code, 400)

        # Cookie API with browser fallback setting
        r_put = client.put("/api/youtube-cookies", json={"browser": "firefox"}, headers=origin)
        self.assertEqual(r_put.status_code, 200)
        self.assertEqual(r_put.json()["browser"], "firefox")
        self.assertEqual(config.get_yt_browser(), "firefox")

        r_get = client.get("/api/youtube-cookies", headers=origin)
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["browser"], "firefox")


if __name__ == "__main__":
    unittest.main()
