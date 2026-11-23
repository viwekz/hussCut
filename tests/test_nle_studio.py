"""Tests for PeakCut NLE Studio extensions:
- Text-based video editing (EDL) & subtitles (SRT/VTT)
- Audio engine (music, SFX, ducking, studio voice)
- Multi-clip mixer & teaser intro
- AI viral scoring & boundary snapping
- Database sequences & clip edits persistence
"""
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import edl, audio, brain, mixer, db, config


class TestEDL(unittest.TestCase):
    def test_calculate_edl_segments_no_cuts(self):
        segments = edl.calculate_edl_segments(0.0, 10.0, [])
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0][0], 0.0)
        self.assertAlmostEqual(segments[0][1], 10.0)

    def test_calculate_edl_segments_with_cuts(self):
        cuts = [[2.0, 4.0], [7.0, 8.5]]
        segments = edl.calculate_edl_segments(0.0, 10.0, cuts)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0], [0.0, 2.0])
        self.assertEqual(segments[1], [4.0, 7.0])
        self.assertEqual(segments[2], [8.5, 10.0])

    def test_calculate_recalculated_words(self):
        words = [
            {"w": "Hello", "s": 0.5, "e": 1.0},
            {"w": "um", "s": 1.2, "e": 1.8},
            {"w": "world", "s": 2.2, "e": 2.8},
        ]
        cuts = [[1.0, 2.0]]
        word_edits = {"2": "WORLD!"}
        recalc = edl.calculate_recalculated_words(words, 0.0, 5.0, cuts, word_edits)
        self.assertEqual(len(recalc), 2)
        self.assertEqual(recalc[0]["w"], "Hello")
        self.assertEqual(recalc[0]["s"], 0.5)
        self.assertEqual(recalc[0]["e"], 1.0)
        # 1.0-2.0 is cut (duration 1.0s), original s=2.2 becomes 2.2 - 1.0 = 1.2
        self.assertEqual(recalc[1]["w"], "WORLD!")
        self.assertAlmostEqual(recalc[1]["s"], 1.2)
        self.assertAlmostEqual(recalc[1]["e"], 1.8)

    def test_find_pauses(self):
        words = [
            {"w": "First", "s": 0.0, "e": 1.0},
            {"w": "Second", "s": 2.0, "e": 3.0},  # 1.0s gap
            {"w": "Third", "s": 3.2, "e": 4.0},   # 0.2s gap
        ]
        pauses = edl.find_pauses(words, 0.0, 5.0, min_gap=0.5)
        self.assertEqual(len(pauses), 2)
        self.assertAlmostEqual(pauses[0][0], 1.05)
        self.assertAlmostEqual(pauses[0][1], 1.95)

    def test_find_filler_words(self):
        words = [
            {"w": "Hey", "s": 0.0, "e": 0.5},
            {"w": "um,", "s": 0.6, "e": 1.0},
            {"w": "actually", "s": 1.1, "e": 1.5},
            {"w": "like", "s": 1.6, "e": 2.0},
        ]
        fillers = edl.find_filler_words(words, 0.0, 3.0)
        self.assertTrue(len(fillers) >= 2)
        self.assertEqual(fillers[0], [0.58, 1.02])

    def test_generate_srt_and_vtt(self):
        words = [
            {"w": "Welcome", "s": 0.0, "e": 0.5},
            {"w": "to", "s": 0.6, "e": 0.8},
            {"w": "PeakCut", "s": 0.9, "e": 1.5},
        ]
        srt = edl.generate_srt(words, max_words_per_line=3)
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
        self.assertIn("Welcome to PeakCut", srt)

        vtt = edl.generate_vtt(words, max_words_per_line=3)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", vtt)


class TestAudioEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pk_test_audio_")
        self.orig_data = config.DATA_DIR
        config.DATA_DIR = Path(self.temp_dir)

    def tearDown(self):
        config.DATA_DIR = self.orig_data
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ensure_audio_assets(self):
        audio.ensure_audio_assets()
        music = audio.list_music()
        sfx = audio.list_sfx()
        self.assertTrue(len(music) >= 2)
        self.assertTrue(len(sfx) >= 4)
        names_m = [m["name"] for m in music]
        ids_s = [s["id"] for s in sfx]
        self.assertIn("Chill Synth", names_m)
        self.assertIn("whoosh", ids_s)
        self.assertIn("censor", ids_s)

    def test_build_audio_filtergraph(self):
        # Test studio voice preset without music
        fg, out = audio.build_audio_filtergraph(has_music=False, studio_voice=True)
        self.assertIn("highpass=f=80", fg)
        self.assertIn("loudnorm", fg)

        # Test ducking & sfx
        fg_complex, out_lbl = audio.build_audio_filtergraph(
            has_music=True,
            music_filename="chill_synth.wav",
            music_vol=0.25,
            ducking=True,
            sfx_list=[{"filename": "whoosh.wav", "offset": 1.5, "volume": 0.8}],
            studio_voice=True
        )
        self.assertIn("sidechaincompress", fg_complex)
        self.assertIn("adelay=1500", fg_complex)
        self.assertIn("amix=inputs=", fg_complex)


class TestBrainAndScoring(unittest.TestCase):
    def test_snap_boundaries(self):
        words = [
            {"w": "Hi", "s": 1.0, "e": 1.3},
            {"w": "there", "s": 1.4, "e": 1.8},
            {"w": "friends", "s": 2.4, "e": 2.9},  # gap 1.8 -> 2.4 (0.6s)
            {"w": "bye.", "s": 4.0, "e": 4.5},
        ]
        s, e = brain.snap_boundaries(2.0, 4.2, words, min_pause=0.25, breath_buffer=0.15)
        self.assertTrue(s <= 2.4)
        self.assertTrue(e >= 4.2)

    def test_tidy_viral_scoring_and_variants(self):
        raw = {
            "title": "Crazy Discovery",
            "hook": "You won't believe what we found!",
            "viral_score": 88,
            "hook_strength": 24,
            "energy_arc": 22,
            "context_clarity": 20,
            "payoff_retention": 22,
            "reason": "Intense curiosity gap and clear reveal.",
            "hook_variants": ["Guess what happened?", "Wait until the end", "Mind blown"],
            "title_variants": ["Wild Secret", "Shocking Truth", "Unreal Moment"]
        }
        cleaned = brain._tidy(raw, 5.0, 25.0)
        self.assertEqual(cleaned["viral_score"], 88)
        self.assertEqual(len(cleaned["hook_variants"]), 3)
        self.assertEqual(len(cleaned["title_variants"]), 3)
        self.assertIn("viral_breakdown", cleaned)
        self.assertEqual(cleaned["viral_breakdown"]["hook_strength"], 24)


class TestMixerAndSequencer(unittest.TestCase):
    def test_add_climax_teaser_intro(self):
        clips = [
            {"job_id": "j1", "clip_id": "c1", "start": 0.0, "end": 10.0, "viral_score": 70},
            {
                "job_id": "j1",
                "clip_id": "c2",
                "start": 10.0,
                "end": 25.0,
                "viral_score": 95,
                "title": "Insane Boss Fight"
            }
        ]
        seq = mixer.add_climax_teaser_intro(clips)
        self.assertEqual(len(seq), 3)
        # The teaser intro must be at index 0
        self.assertTrue(seq[0]["is_teaser"])
        self.assertEqual(seq[0]["transition"], "fade")
        self.assertEqual(seq[1]["clip_id"], "c1")
        self.assertEqual(seq[2]["clip_id"], "c2")


class TestDatabaseExtensions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="pk_test_db_")
        self.orig_data = config.DATA_DIR
        config.DATA_DIR = Path(self.temp_dir)
        db.init()

    def tearDown(self):
        config.DATA_DIR = self.orig_data
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clip_edits_crud(self):
        edit_edl = {"cuts": [[1.0, 2.5]]}
        edit_style = {"font": "Impact", "size": 32, "active_color": "#00ffcc"}
        edit_audio = {"music": "chill_synth", "ducking": True, "studio_voice": True}
        db.save_clip_edit("user1", "job1", "clip1", edl=edit_edl, style=edit_style, audio=edit_audio)
        retrieved = db.get_clip_edit("user1", "job1", "clip1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["audio"]["music"], "chill_synth")
        self.assertEqual(len(retrieved["edl"]["cuts"]), 1)

    def test_sequence_crud(self):
        items = [{"clip_id": "c1", "job_id": "j1", "transition": "fade"}]
        seq_audio = {"music": "lofi_vibe", "volume": 0.3}
        saved = db.save_sequence("userA", "seq123", title="My Viral Sequence", aspect="9:16", items=items, audio=seq_audio)
        self.assertEqual(saved["id"], "seq123")
        
        all_seqs = db.list_sequences("userA")
        self.assertEqual(len(all_seqs), 1)
        self.assertEqual(all_seqs[0]["title"], "My Viral Sequence")

        fetched = db.get_sequence("userA", "seq123")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["items"][0]["transition"], "fade")

        deleted = db.delete_sequence("userA", "seq123")
        self.assertTrue(deleted)
        self.assertEqual(len(db.list_sequences("userA")), 0)


if __name__ == "__main__":
    unittest.main()
