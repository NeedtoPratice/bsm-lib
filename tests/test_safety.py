#!/usr/bin/env python3
"""Content-preservation regressions; all generated data lives in /tmp."""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bsm as M
from mutagen.id3 import ID3, SYLT, TEXT


class ParsingSafetyTests(unittest.TestCase):
    def test_real_sentences_are_not_placeholders(self):
        for text in (
            "鼓声响起", "吉他伴我走", "钢琴声渐远", "演唱我们的歌",
            "You were instrumental in my life", "There are no lyrics to explain",
            "这首歌没有歌词也能表达爱", "Producer of my dreams",
        ):
            with self.subTest(text=text):
                self.assertTrue(M.has_real_lyrics_in_text(f"[00:01.00]{text}\n"))

    def test_known_notices_and_explicit_credits_are_not_lyrics(self):
        for text in (
            "No lyrics", "No Lyrics!", "纯音乐，请欣赏", "纯器乐，请欣赏。",
            "暂无歌词", "Instrumental", "作词：Someone", "吉他：Someone",
            "Written by: Someone", "[ar:Artist [name]]",
        ):
            with self.subTest(text=text):
                self.assertFalse(M.has_real_lyrics_in_text(text))

    def test_repeated_original_gives_every_continuation_every_time(self):
        source = "[00:10.00][00:30.00]かな\nkana\n中文\n[00:40.00]\n"
        expected = (
            "[00:10.00]かな\n[00:10.00]kana\n[00:10.00]中文\n"
            "[00:30.00]かな\n[00:30.00]kana\n[00:30.00]中文\n[00:40.00]\n"
        )
        result, dropped = M.bilingual_process_lrc(source, None)
        self.assertEqual(result, expected)
        self.assertEqual(dropped, 0)
        self.assertIsNone(M.bilingual_process_lrc(result, None)[0])

    def test_large_minutes_keep_their_time(self):
        source = "[00:01.00]Intro\n[100:00.00]Finale\n译文\n"
        result, _ = M.bilingual_process_lrc(source, None)
        self.assertEqual(result, "[00:01.00]Intro\n[100:00.00]Finale\n[100:00.00]译文\n")
        self.assertEqual(M.pad_bare_timestamp("100:00"), "100:00.00")
        self.assertEqual(M.pad_line_timestamps("[100:00.5]Finale", 3), "[100:00.500]Finale")

    def test_unsupported_time_is_not_adopted_as_a_continuation(self):
        source = ["[00:01.00]Intro", "[00:02.1234]Opaque", "Uncertain continuation"]
        self.assertEqual(M.adopt_untimed_lines(source), source)

    def test_mixed_valid_invalid_timestamps_survive_romaji_reorder(self):
        source = (
            "[00:01.00]かな\n[00:01.00]中文\n[00:01.00]kana\n"
            "[00:02.00][00:75.00]repeat\n"
        )
        result = M.romaji_process_lrc(source)
        self.assertIsNotNone(result)
        self.assertIn("[00:02.00][00:75.00]repeat\n", result)
        events = lambda text: Counter(e for line in text.splitlines() for e in M.raw_timed_entries(line))
        self.assertEqual(events(source), events(result))
        self.assertIsNone(M.romaji_process_lrc(result))

    def test_extraction_preserves_unparsed_text_and_timed_blanks(self):
        source = "Preface\n[ti:Title]\n[00:01.00]Original\n译文\n[00:75.00]Odd time\n[00:80.00]\n"
        entries, meta = M.parse_embedded_lyrics(source)
        result = M.build_lrc(entries, None, None, None, None, meta)
        for line in ("Preface", "[ti:Title]", "[00:01.00]译文", "[00:75.00]Odd time", "[00:80.00]"):
            with self.subTest(line=line):
                self.assertIn(line + "\n", result)

    def test_extraction_still_skips_fully_untimed_lyrics(self):
        self.assertEqual(M.parse_embedded_lyrics("[ti:Title]\nPlain lyric\n")[0], [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for audio fixtures")
class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bsm-safety-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "music"
        self.library.mkdir()
        self.args = M.parse_args([str(self.library)])
        self.args.music_root = self.library.resolve()
        self.args.apply = True
        self.args.write_reports = True
        self.args.report_dir = self.root / "reports"
        self.args.backup_dir = self.root / "backups"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def audio(self, stem="Old - Artist", suffix=".mp3"):
        path = self.library / (stem + suffix)
        codec = {".mp3": "libmp3lame", ".flac": "flac", ".ogg": "libvorbis", ".m4a": "aac"}[suffix]
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.1", "-map_metadata", "-1",
            "-metadata", "title=New", "-metadata", "artist=Artist", "-c:a", codec, str(path),
        ], check=True, capture_output=True)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    @staticmethod
    def mp3_payload(path):
        data = path.read_bytes()
        if data[:3] == b"ID3":
            size = 0
            for byte in data[6:10]:
                size = (size << 7) | byte
            data = data[10 + size:]
        return data[:-128] if data[-128:-125] == b"TAG" else data

    def test_dry_run_never_writes_even_with_yes_and_backup(self):
        audio = self.audio()
        audio.with_suffix(".lrc").write_text(
            "[ti:Title]\n[00:01.1]作词：Someone\n[00:02.22]かな\n中文\nkana\n",
            encoding="utf-8",
        )
        before = self.snapshot()
        for extra in ([], ["--yes"]):
            with self.subTest(extra=extra), patch.object(M, "DEFAULT_BACKUP_DIR", self.args.backup_dir), patch.object(M, "refresh_mpd") as refresh:
                result = M.main([
                    str(self.library), "--dry-run", "--backup", "--report-dir",
                    str(self.args.report_dir), *extra,
                ])
                self.assertEqual(result, 0)
                refresh.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.args.report_dir.exists())
                self.assertFalse(self.args.backup_dir.exists())

    def test_nolyrics_keeps_real_sentences(self):
        audio = self.audio()
        lrc = audio.with_suffix(".lrc")
        for content in (
            "[00:01.00]鼓声响起\n[00:02.00]吉他伴我走\n",
            "[00:01.00]You were instrumental in my life\n",
        ):
            with self.subTest(content=content):
                lrc.write_text(content, encoding="utf-8")
                self.assertEqual(M.process_no_lyrics(audio, self.args), "skipped_has_lyrics")
                self.assertEqual(lrc.read_text(encoding="utf-8"), content)

    def test_extract_to_embed_preserves_translations_in_all_containers(self):
        embedded = "[00:01.00][00:02.00]かな\nkana\n中文\n[00:03.00]\n"
        expected = (
            "[00:01.00]かな\n[00:01.00]kana\n[00:01.00]中文\n"
            "[00:02.00]かな\n[00:02.00]kana\n[00:02.00]中文\n[00:03.00]\n"
        )
        for suffix in (".mp3", ".flac", ".ogg", ".m4a"):
            with self.subTest(suffix=suffix):
                audio = self.audio("sample-" + suffix[1:], suffix)
                M.write_embedded_lyrics(audio, embedded)
                self.assertEqual(M.process_extract(audio, self.args)[0], "written")
                for runner in (M.step_clean, M.step_bilingual, M.step_romaji, M.step_stamps, M.step_embed):
                    result = runner(self.args, self.library)
                    self.assertFalse(result.errors)
                self.assertEqual(audio.with_suffix(".lrc").read_text(encoding="utf-8"), expected)
                self.assertEqual(M.extract_embedded_text(audio), expected)

    def test_clean_backs_up_original_tags_and_exact_lrc_before_embed(self):
        audio = self.audio()
        tags = ID3(audio)
        tags.add(TEXT(encoding=3, text=["Original credit"]))
        tags.save(audio)
        lrc = audio.with_suffix(".lrc")
        original = "\ufeff[00:00.00]作词：Replacement credit\n[00:01.00]Real lyric\n"
        lrc.write_text(original, encoding="utf-8")
        self.args.backup = True
        payload = self.mp3_payload(audio)
        self.assertFalse(M.step_clean(self.args, self.library).errors)
        backup = self.args.backup_dir / (audio.name + ".tags.txt")
        before_embed = backup.read_bytes()
        self.assertIn("Original credit", before_embed.decode("utf-8"))
        self.assertNotIn("Replacement credit", before_embed.decode("utf-8"))
        self.assertEqual((self.args.backup_dir / lrc.name).read_bytes(), original.encode("utf-8"))
        self.assertEqual(ID3(audio).getall("TEXT")[0].text, ["Replacement credit"])
        self.assertFalse(M.step_embed(self.args, self.library).errors)
        self.assertEqual(backup.read_bytes(), before_embed)
        self.assertEqual(self.mp3_payload(audio), payload)

    def test_clean_does_not_write_tags_when_backup_fails(self):
        audio = self.audio()
        lrc = audio.with_suffix(".lrc")
        lrc.write_text("[00:01.00]作词：Someone\n[00:02.00]Real lyric\n", encoding="utf-8")
        self.args.backup = True
        self.args.backup_dir.write_text("Not a directory", encoding="utf-8")
        before = (audio.read_bytes(), lrc.read_bytes())
        result = M.step_clean(self.args, self.library)
        self.assertEqual(result.counts["error"], 1)
        self.assertEqual((audio.read_bytes(), lrc.read_bytes()), before)

    def test_lyric_edit_stops_when_text_backup_fails(self):
        lrc = self.library / "sample.lrc"
        original = "[00:01.1]First\n[00:02.22]Second\n"
        lrc.write_text(original, encoding="utf-8")
        self.args.backup = True
        self.args.backup_dir.write_text("Not a directory", encoding="utf-8")
        result = M.step_stamps(self.args, self.library)
        self.assertEqual(result.counts["error"], 1)
        self.assertEqual(lrc.read_text(encoding="utf-8"), original)

    def test_embed_replaces_old_sylt_and_is_idempotent(self):
        audio = self.audio()
        tags = ID3(audio)
        tags.add(SYLT(encoding=3, lang="eng", format=2, type=1, desc="old", text=[("Old lyric", 1000)]))
        tags.save(audio)
        content = "[00:01.00]Correct new lyrics\n"
        audio.with_suffix(".lrc").write_text(content, encoding="utf-8")
        payload = self.mp3_payload(audio)
        result = M.step_embed(self.args, self.library)
        self.assertFalse(result.errors)
        self.assertEqual(result.counts["embedded"], 1)
        self.assertFalse(ID3(audio).getall("SYLT"))
        self.assertEqual(M.extract_embedded_text(audio), content)
        once = audio.read_bytes()
        result = M.step_embed(self.args, self.library)
        self.assertEqual(result.counts["embedded"], 0)
        self.assertEqual(result.counts["skipped_identical"], 1)
        self.assertEqual(audio.read_bytes(), once)
        self.assertEqual(self.mp3_payload(audio), payload)

    def assert_rename_collision_keeps_pair(self, with_source_lrc):
        audio = self.audio()
        lrc = audio.with_suffix(".lrc")
        if with_source_lrc:
            lrc.write_text("[00:01.00]Correct lyrics\n", encoding="utf-8")
        target_lrc = self.library / "New - Artist.lrc"
        target_lrc.write_text("[00:01.00]Unrelated lyrics\n", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.library.iterdir()}
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.args.apply = apply
                result = M.step_rename(self.args, self.library)
                self.assertFalse(result.errors)
                self.assertEqual(result.counts["renamed"], 0)
                self.assertEqual(result.counts["skipped_conflict"], 1)
                self.assertEqual({p.name: p.read_bytes() for p in self.library.iterdir()}, before)

    def test_rename_refuses_existing_target_lyrics(self):
        self.assert_rename_collision_keeps_pair(True)

    def test_rename_refuses_wrong_pairing_even_without_source_lyrics(self):
        self.assert_rename_collision_keeps_pair(False)

    def test_rename_rolls_back_audio_if_lyric_rename_fails(self):
        audio = self.audio()
        lrc = audio.with_suffix(".lrc")
        lrc.write_text("[00:01.00]Correct lyrics\n", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.library.iterdir()}
        rename = Path.rename

        def fail_lyric(path, target):
            if path == lrc:
                raise PermissionError("Injected lyric rename failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_lyric):
            result = M.step_rename(self.args, self.library)
        self.assertEqual(result.counts["error"], 1)
        self.assertEqual(result.counts["renamed"], 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.library.iterdir()}, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
