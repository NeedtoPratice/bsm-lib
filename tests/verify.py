"""Read-only check that the library actually matches the standard.

`audit.py` asks "what would a run still change?"; this asks "did the run work?".
Run it after a full pass: every counter should be 0.

    python3 tests/verify.py [music_dir]

Never writes anything.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # musicManage/
import music_lib as M

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else M.DEFAULT_MUSIC_DIR).expanduser()

problems: dict[str, list[str]] = {}


def bad(key: str, detail: str) -> None:
    problems.setdefault(key, []).append(detail)


audio = M.audio_files(ROOT)
lrcs = M.lrc_files(ROOT)
audio_stems = {p.with_suffix("") for p in audio}
lrc_stems = {p.with_suffix("") for p in lrcs}

for stem in sorted(lrc_stems - audio_stems):
    bad("lrc without audio", stem.name)
for stem in sorted(audio_stems - lrc_stems):
    bad("audio without lrc", stem.name)

# --- the .lrc side ---------------------------------------------------------
for lrc in lrcs:
    text = lrc.read_text(encoding="utf-8", errors="replace")
    if text.startswith("\ufeff"):
        bad("bom", lrc.name)
        text = text[1:]
    if not text.endswith("\n"):
        bad("no trailing newline", lrc.name)

    if text == M.build_no_lyrics_lrc():
        continue  # a lyric-less file, in its finished shape

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue  # blank lines are kept as they are
        if M.LRC_HEADER_RE.match(s):
            bad("header tag left", f"{lrc.name}: {s[:40]}")
            continue
        split = M.split_lrc_line(line)
        if not split:
            bad("untimed line left", f"{lrc.name}: {s[:40]}")
            continue
        timestamps, content = split
        if len(timestamps) > 1:
            bad("multi-timestamp line", f"{lrc.name}: {s[:40]}")
        if any(M.normalize_timestamp(ts) is None for ts in timestamps):
            bad("unrepresentable timestamp", f"{lrc.name}: {s[:40]}")
        if content and M.matching_notice(content):
            bad("notice left", f"{lrc.name}: {content[:40]}")

    # every timestamped group must be adjacent and share one timestamp
    if M.bilingual_process_lrc(text, None)[0] is not None:
        bad("bilingual would still change it", lrc.name)
    if M.romaji_process_lrc(text) is not None:
        bad("romaji would still change it", lrc.name)

# --- the audio side --------------------------------------------------------
for lrc in lrcs:
    p = M.find_audio_for_lrc(lrc)
    if p is None:
        continue
    text = lrc.read_text(encoding="utf-8", errors="replace")
    embedded = M.extract_embedded_text(p)
    if embedded is None or embedded.strip() != text.strip():
        bad("embedded != .lrc", p.name)

for p in audio:
    title, artist, _notes = M.desired_name_fields(p)
    if not title:
        bad("no title tag", p.name)
        continue
    wanted = (f"{title} - {artist}" if artist else title).strip().rstrip(".")
    problem = M.name_problem(wanted)
    if problem:
        continue  # e.g. a "/" in the title: deliberately left alone
    if p.stem != wanted:
        bad("name != '<title> - <artist>'", f"{p.stem}  (want {wanted})")

print(f"library      : {ROOT}")
print(f"audio / .lrc : {len(audio)} / {len(lrcs)}")
print()
if not problems:
    print("OK -- every check passed: the library matches the standard.")
    raise SystemExit(0)

total = sum(len(v) for v in problems.values())
for key in sorted(problems, key=lambda k: -len(problems[k])):
    items = problems[key]
    print(f"{len(items):5}  {key}")
    for item in items[:6]:
        print(f"         {item}")
    if len(items) > 6:
        print(f"         ... and {len(items) - 6} more")
print(f"\n{total} problem(s).")
raise SystemExit(1)
