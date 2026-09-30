"""Read-only audit of the real MPD library against music_lib.py.

Reports both the fixed-point status (would any step change anything?) and the
exposure to the line-loss class of bug (lines the steps cannot represent).
Runs no writes: safe on the live library.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # musicManage/
import music_lib as M

ROOT = Path("/home/NeedtoPratice/Music")
lrcs = M.lrc_files(ROOT)
audio = M.audio_files(ROOT)

print(f"audio files            : {len(audio)}")
print(f".lrc files             : {len(lrcs)}")
print(f".lrc without audio     : {sum(1 for p in lrcs if M.find_audio_for_lrc(p) is None)}")
print(f"audio without .lrc     : {sum(1 for p in audio if not p.with_suffix('.lrc').exists())}")

stats = {k: 0 for k in (
    "bom", "untimed_body_line", "unrepresentable_ts", "multi_ts_line",
    "droppable_tag", "late_meta_tag", "repeated_ts", "bilingual_would_write",
    "romaji_would_write", "placeholder", "placeholder_would_rewrite",
)}
examples: dict[str, list[str]] = {k: [] for k in stats}


def note(key: str, text: str) -> None:
    stats[key] += 1
    if len(examples[key]) < 5:
        examples[key].append(text)


for p in lrcs:
    raw = p.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("\ufeff"):
        note("bom", p.name)
    text = raw[1:] if raw.startswith("\ufeff") else raw

    split = M.split_header_and_body(text)
    if split is not None:
        header, body = split
        for ln in body:
            s = ln.strip()
            if not s:
                continue
            entries = M.split_lrc_line(ln)
            if not entries:
                note("untimed_body_line", f"{p.name}: {s[:50]}")
                continue
            if len(entries[0]) > 1:
                note("multi_ts_line", f"{p.name}: {s[:50]}")
            if any(M.normalize_timestamp(ts) is None for ts in entries[0]):
                note("unrepresentable_ts", f"{p.name}: [{entries[0][0]}]")
            if M.LRC_META_RE.match(s):
                note("late_meta_tag", f"{p.name}: {s[:50]}")

    for ln in text.splitlines():
        m = M.LRC_META_RE.match(ln.strip())
        if m and m.group(1).lower() in M.DROPPABLE_LRC_TAGS:
            note("droppable_tag", f"{p.name}: {ln.strip()}")
            break

    if M.has_repeated_timestamp(text):
        stats["repeated_ts"] += 1
    if M.bilingual_process_lrc(text, None)[0] is not None:
        note("bilingual_would_write", p.name)
    if M.romaji_process_lrc(text) is not None:
        note("romaji_would_write", p.name)

    if not M.has_real_lyrics_in_text(text):
        stats["placeholder"] += 1
        a = M.find_audio_for_lrc(p)
        if a is not None:
            artist, title, album, length = M.get_metadata(a)
            want = M.build_no_lyrics_lrc(
                artist, title, album, length, M.existing_lrc_header(text)
            )
            if want != text:
                note("placeholder_would_rewrite", p.name)

print()
for k, v in stats.items():
    tail = ("   e.g. " + "; ".join(examples[k])) if examples[k] else ""
    print(f"{k:26}: {v}{tail}")

shapes: dict[str, int] = {}
for p in lrcs:
    text = p.read_text(encoding="utf-8", errors="replace")
    split = M.split_header_and_body(text)
    if not split:
        continue
    groups: dict[int, list[str]] = {}
    for ln in split[1]:
        for ts, content in M.parse_lrc_line(ln):
            ms = M.timestamp_to_ms(ts)
            if ms is not None:
                groups.setdefault(ms, []).append(content)
    for contents in groups.values():
        if len(contents) < 2:
            continue
        kind = "".join(
            "C" if M.is_chinese(c) else ("R" if M.is_romaji(c) else "O") for c in contents
        )
        shapes[kind] = shapes.get(kind, 0) + 1

print("\ngroup language shapes (C=chinese R=romaji O=other):")
for k, v in sorted(shapes.items(), key=lambda kv: -kv[1])[:10]:
    print(f"  {k:6} {v}")
