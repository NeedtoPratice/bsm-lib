"""Read-only audit of the real MPD library against music_lib.py.

Reports, per the *current* rules:

* how far the library is from the standard (headers, notices, untimed
  continuations, placeholder shape), i.e. what each step would still change;
* the exposure to the line-loss class of bug -- lines the steps cannot represent.

Runs no writes and no backups: safe on the live library.  It is the read-only
counterpart of an interactive run, so it answers "what would happen?" without
ever asking to apply anything.
"""
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # musicManage/
import music_lib as M

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else M.DEFAULT_MUSIC_DIR).expanduser()
lrcs = M.lrc_files(ROOT)
audio = M.audio_files(ROOT)

print(f"audio files            : {len(audio)}")
print(f".lrc files             : {len(lrcs)}")
print(f".lrc without audio     : {sum(1 for p in lrcs if M.find_audio_for_lrc(p) is None)}")
print(f"audio without .lrc     : {sum(1 for p in audio if not p.with_suffix('.lrc').exists())}")
print(f"declared instrumental  : {len(M.MANUAL_INSTRUMENTAL)}")
print(f"rules loaded           : {len(M.CREDIT_ROLES)} roles, "
      f"{len(M.INSTRUMENT_ROLES)} instruments, {len(M.NOTICE_PATTERNS)} notice patterns")

stats: collections.Counter[str] = collections.Counter()
reasons: collections.Counter[str] = collections.Counter()
examples: dict[str, list[str]] = collections.defaultdict(list)


def note(key: str, text: str) -> None:
    stats[key] += 1
    if len(examples[key]) < 4:
        examples[key].append(text)


for p in lrcs:
    raw = p.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("\ufeff"):
        note("bom", p.name)
    text = raw[1:] if raw.startswith("\ufeff") else raw

    split = M.split_header_and_body(text)
    if split is not None:
        _header, body = split
        for ln in body:
            s = ln.strip()
            if not s:
                continue
            if not M.split_lrc_line(ln):
                note("untimed_body_line", f"{p.name}: {s[:46]}")
                continue
            entries = M.split_lrc_line(ln)
            if entries and len(entries[0]) > 1:
                note("multi_ts_line", f"{p.name}: {s[:46]}")
            if entries and any(M.normalize_timestamp(ts) is None for ts in entries[0]):
                note("unrepresentable_ts", f"{p.name}: {s[:46]}")

    # What the cleaner would take out, by reason.
    artist_tag = M.get_metadata(M.find_audio_for_lrc(p))[0] if M.find_audio_for_lrc(p) else None
    artist_names = [a.strip() for a in re.split(r"[;,/]", artist_tag)] if artist_tag else []
    plan = M.plan_credit_extraction(
        text.splitlines(), artist_names, forced_instrumental=p.name in M.MANUAL_INSTRUMENTAL
    )
    if plan.remove_idx:
        note("clean_would_remove", f"{p.name} ({len(plan.remove_idx)} lines)")
    for entry in plan.removed_preview:
        reason = entry.rsplit("(", 1)[-1].rstrip(")")
        reasons[reason.split("  /")[0]] += 1

    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = M.TIMESTAMP_RE.match(s)
        candidate = s[m.end():].strip() if m else s
        if candidate and M.matching_notice(candidate):
            note("notice_line", f"{p.name}: {candidate[:46]}")

    if M.has_repeated_timestamp(text):
        stats["repeated_ts"] += 1
    if M.bilingual_process_lrc(text, None)[0] is not None:
        note("bilingual_would_write", p.name)
    if M.romaji_process_lrc(text) is not None:
        note("romaji_would_write", p.name)

    if not M.has_real_lyrics_in_text(text):
        stats["placeholder"] += 1
        if text != M.build_no_lyrics_lrc():
            note("placeholder_not_single_line", p.name)

print()
for key in (
    "clean_would_remove", "notice_line", "untimed_body_line", "multi_ts_line",
    "unrepresentable_ts", "bilingual_would_write", "romaji_would_write",
    "placeholder", "placeholder_not_single_line", "bom", "repeated_ts",
):
    tail = ("   e.g. " + "; ".join(examples[key])) if examples[key] else ""
    print(f"{key:28}: {stats[key]}{tail}")

print("\nlines the cleaner would remove, by reason:")
for reason, count in reasons.most_common():
    print(f"  {count:5}  {reason}")
