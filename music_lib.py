#!/usr/bin/env python3
"""Unified maintenance for an MPD/rmpc music library.

This single script replaces the former set:

    fix_ogg_titles.py + extract_lrc_rmpc.py + normalize_no_lyrics.py
    + bilingual_lrc.py + reorder_romaji.py + one_click_update.sh

Steps, executed in this order by default:

    ogg        fill missing/empty OGG title tags from the file name
    extract    extract embedded timed lyrics into rmpc-compatible .lrc files
    credits    move author info (作词/作曲/编曲/…) from .lrc into audio tags
    nolyrics   write "[00:00.00]No lyrics" for songs without real lyrics
    bilingual  group same-timestamp lyric lines consecutively
    romaji     move romaji lines between the original and the Chinese line

After the steps, the MPD database is refreshed with ``rmpc update`` (falling
back to ``mpc update``) unless ``--no-refresh`` or ``--dry-run`` is used.

Every step rewrites only what it understands: lines it cannot parse (untimed
text, timestamps out of range, stray header tags) are preserved verbatim, and a
file that fails to write is reported without stopping the remaining files.

Examples:
    ./music_lib.py                                  # full pipeline
    ./music_lib.py --dry-run                        # show, change nothing
    ./music_lib.py /path/to/Music --steps extract,bilingual
    ./music_lib.py --steps ogg --backup
    ./music_lib.py --steps bilingual --only-duplicates
    ./music_lib.py --steps bilingual --max-lines 2
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TCOM, TEXT, TIPL, TMCL, TXXX, IPLS, ID3NoHeaderError
    from mutagen.oggvorbis import OggVorbis
except ImportError as _e:  # pragma: no cover - environment guard
    print(f"Error: could not import mutagen ({_e}). Try: pip install mutagen", file=sys.stderr)
    raise SystemExit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MUSIC_DIR = "/home/NeedtoPratice/Music"
# Everything this tool generates stays inside musicManage/: review reports here,
# backups under backups/<timestamp>/ (see README.md).
DEFAULT_REPORT_DIR = SCRIPT_DIR / "reports"

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".mp4", ".m4b"}

# How many removed lines a --dry-run shows per file before collapsing.
DRY_RUN_PREVIEW_LINES = 8

# Matches [MM:SS], [MM:SS.x], [MM:SS.xx], [MM:SS.xxx], [MM:SS:xx]
TIMESTAMP_RE = re.compile(r"\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]")

# A line is "no real lyrics" when its normalized text contains one of these.
PLACEHOLDER_KEYWORDS = [
    "纯音乐",
    "无歌词",
    "没有歌词",
    "暂无歌词",
    "纯器乐",
    "nolyrics",
    "nolyric",
    "instrumental",
]

# Credit/metadata lines that often appear in "instrumental / no lyrics" LRC
# files.  They are not actual song lyrics.
NON_LYRIC_KEYWORDS = [
    "作词",
    "作曲",
    "编曲",
    "制作人",
    "演唱",
    "混音",
    "录音",
    "监制",
    "出品",
    "发行",
    "和声",
    "贝斯",
    "吉他",
    "鼓",
    "键盘",
    "弦乐",
    "小提琴",
    "钢琴",
    "writtenby",
    "composedby",
    "composer",
    "lyricsby",
    "musicby",
    "producedby",
    "producer",
    "arrangedby",
    "arranger",
    "vocalsby",
    "mixedby",
    "recordedby",
    "engineer",
    "masteredby",
]

STEP_ORDER = ["ogg", "extract", "credits", "nolyrics", "bilingual", "romaji"]


# ---------------------------------------------------------------------------
# Shared LRC timestamp helpers
# ---------------------------------------------------------------------------


def parse_timestamp_parts(ts: str) -> tuple[int, int, str] | None:
    """Split an LRC timestamp into (minutes, seconds, fraction_string)."""
    ts = ts.strip().strip("[]")
    if ":" not in ts:
        return None
    minutes, rest = ts.split(":", 1)
    if ":" in rest:
        seconds, fraction = rest.split(":", 1)
    elif "." in rest:
        seconds, fraction = rest.split(".", 1)
    else:
        seconds, fraction = rest, ""

    if not minutes.isdigit() or not seconds.isdigit():
        return None
    # An empty fraction means a bare [MM:SS] timestamp, which is valid and
    # gets padded to .00 later; any other non-digit fraction is rejected.
    if fraction and not fraction.isdigit():
        return None
    return int(minutes), int(seconds), fraction


def fraction_to_ms(fraction: str) -> int:
    """Scale a fraction string to milliseconds."""
    if len(fraction) == 0:
        return 0
    if len(fraction) == 1:
        return int(fraction) * 100
    if len(fraction) == 2:
        return int(fraction) * 10
    return int(fraction[:3])


def normalize_timestamp(ts: str) -> str | None:
    """Return a standard [MM:SS.ff] timestamp, or None if invalid.

    Also converts [00:24] to [00:24.00] and [00:04:73] to [00:04.73],
    matching what rmpc can parse reliably.
    """
    parts = parse_timestamp_parts(ts)
    if parts is None:
        return None
    minutes, seconds, fraction = parts
    if seconds > 59:
        # rmpc allows large minute values, but seconds should still be < 60
        # for a sane LRC file.  Stay strict here to avoid writing junk.
        return None
    if len(fraction) == 0:
        fraction_out = "00"
    elif len(fraction) == 1:
        fraction_out = fraction + "0"
    elif len(fraction) == 2:
        fraction_out = fraction
    else:
        fraction_out = fraction[:3]  # rmpc truncates to milliseconds
    return f"[{minutes:02d}:{seconds:02d}.{fraction_out}]"


def timestamp_to_ms(ts: str) -> int | None:
    """Convert any LRC timestamp to milliseconds (lenient: no seconds limit).

    Fraction digits are scaled positionally: .5 -> 500 ms, .50 -> 500 ms,
    .500 -> 500 ms.
    """
    parts = parse_timestamp_parts(ts)
    if parts is None:
        return None
    minutes, seconds, fraction = parts
    return minutes * 60_000 + seconds * 1000 + fraction_to_ms(fraction)


def split_lrc_line(line: str) -> tuple[list[str], str] | None:
    """Return (raw_timestamps, content) for a line starting with timestamps.

    Handles multiple timestamps on the same line:
        [00:01.00][00:02.00]chorus
    Non-timestamp tags after the timestamps stay part of the content, matching
    rmpc's behaviour.  Timestamps are returned unwrapped and unmodified.
    """
    s = line.strip()
    if not s.startswith("["):
        return None

    timestamps: list[str] = []
    pos = 0
    while s.startswith("[", pos):
        m = TIMESTAMP_RE.match(s, pos)
        if not m:
            break
        timestamps.append(m.group(1))
        pos = m.end()

    if not timestamps:
        return None
    return timestamps, s[pos:].strip()


def parse_lrc_line(line: str) -> list[tuple[str, str]]:
    """Parse one line into (normalized [MM:SS.ff] timestamp, content) pairs.

    Timestamps that cannot be represented as a sane LRC timestamp are dropped.
    """
    split = split_lrc_line(line)
    if split is None:
        return []
    timestamps, content = split
    result: list[tuple[str, str]] = []
    for ts in timestamps:
        norm = normalize_timestamp(ts)
        if norm is not None:
            result.append((norm, content))
    return result


def raw_timed_entries(line: str) -> list[tuple[str, str]]:
    """Parse one line into (raw_timestamp, content) pairs, dropping nothing."""
    split = split_lrc_line(line)
    if split is None:
        return []
    timestamps, content = split
    return [(ts, content) for ts in timestamps]


def pad_bare_timestamp(ts: str) -> str:
    """Add .00 to a bare [MM:SS], leaving any existing fraction untouched.

    This is the historical bilingual_lrc.py spelling: [00:12] -> [00:12.00],
    while [00:12.3] stays [00:12.3].  Preserving it keeps a merge of the old
    scripts behaviour-neutral instead of rewriting files over zero-padding.
    """
    if re.fullmatch(r"\d{1,2}:\d{2}", ts):
        return f"{ts}.00"
    return ts


def regroup_body_lines(
    body: list[str],
    parse: Callable[[str], list[tuple[str, str]]],
    output_timestamp: Callable[[list[tuple[str, str]]], str],
    reorder: Callable[[list[tuple[str, str]]], list[tuple[str, str]]] | None = None,
    max_lines: int | None = None,
) -> tuple[list[str], int, int]:
    """Pull same-timestamp lines together, keeping every other line untouched.

    Only lines the parser understands are regrouped.  Every other body line is
    copied through verbatim, because a real .lrc can hold lines this module
    cannot represent -- an untimed note, a timestamp with seconds > 59 such as
    ``[00:75.00]``, a header tag that appears after the first lyric.  Rebuilding
    the body from parsed groups alone would delete all of those silently.

    ``output_timestamp`` receives a whole group and returns the bracketed
    timestamp to print for every line of that group.

    Returns (new_body, groups_reordered, lines_dropped_by_max_lines).
    """
    groups: "OrderedDict[int, list[tuple[str, str]]]" = OrderedDict()
    parsed: list[list[tuple[str, str]]] = []
    for line in body:
        entries = parse(line)
        parsed.append(entries)
        for ts, content in entries:
            ms = timestamp_to_ms(ts)
            if ms is not None:
                groups.setdefault(ms, []).append((ts, content))

    new_body: list[str] = []
    emitted: set[int] = set()
    reordered = 0
    dropped = 0

    for line, entries in zip(body, parsed):
        usable = [(ts, content) for ts, content in entries if timestamp_to_ms(ts) is not None]
        if not usable:
            new_body.append(line)  # untimed or unrepresentable: never drop it
            continue

        for ts, content in usable:
            ms = timestamp_to_ms(ts)
            if ms is None or ms in emitted:
                continue
            emitted.add(ms)

            entries_for_ms = groups[ms]
            group = reorder(entries_for_ms) if reorder is not None else entries_for_ms
            if group != entries_for_ms:
                reordered += 1
            if max_lines is not None and len(group) > max_lines:
                dropped += len(group) - max_lines
                group = group[:max_lines]

            out_ts = output_timestamp(group)
            new_body.extend(f"{out_ts}{content}" for _ts, content in group)

    return new_body, reordered, dropped


def split_header_and_body(text: str) -> tuple[list[str], list[str]] | None:
    """Return (header_lines, body_lines), or None when there is no timed line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if split_lrc_line(line):
            return lines[:i], lines[i:]
    return None


def render(header: list[str], body: list[str], trailing_newline: bool) -> str:
    new_text = "\n".join([*header, *body])
    if trailing_newline:
        new_text += "\n"
    return new_text


# ---------------------------------------------------------------------------
# Shared argument / IO helpers
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Outcome of one maintenance step."""

    name: str
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def backup_text_file(path: Path, original: str) -> bool:
    """Keep a .bak copy of a text file.  Never overwrites an existing backup."""
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        return False
    bak.write_text(original, encoding="utf-8")
    return True


def backup_binary_file(path: Path) -> bool:
    """Keep a .bak copy of an audio file.  Never overwrites an existing backup."""
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        return False
    shutil.copy2(path, bak)
    return True


def write_report(
    report_dir: Path,
    filename: str,
    header: str,
    items: list[str],
    dry_run: bool,
) -> Path | None:
    """Write a review report, or report how many lines it would hold."""
    if not items:
        return None
    if dry_run:
        print(f"  Would write report {filename} ({len(items)} lines)")
        return None
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / filename
        path.write_text(header + "\n\n" + "\n".join(items) + "\n", encoding="utf-8")
        return path
    except OSError as e:
        print(f"  Could not write {filename}: {e}", file=sys.stderr)
        return None


def audio_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTS)


def lrc_files(root: Path) -> list[Path]:
    # Recursive on purpose: extract/nolyrics write sidecars next to the audio
    # file, so a library with subfolders produces .lrc files below the root.
    return sorted(p for p in root.rglob("*") if p.suffix.lower() == ".lrc")


def report_summary(counts: dict[str, int], order: list[tuple[str, str]]) -> None:
    print("\n  === Summary ===")
    for key, label in order:
        print(f"  {label:<32}{counts.get(key, 0)}")


def finish(result: StepResult) -> StepResult:
    for err in result.errors:
        print(f"  ERROR: {err}")
    for note in result.notes:
        print(f"  {note}")
    return result


# ---------------------------------------------------------------------------
# Step 1: fill missing/empty OGG title tags
# ---------------------------------------------------------------------------


def title_from_filename(path: Path) -> str:
    return path.stem.replace("_", " ").strip()


def process_ogg(path: Path, args: argparse.Namespace) -> tuple[str, str | None, bool]:
    """Return (status, detail, backed_up)."""
    try:
        audio = MutagenFile(path)
        if audio is None:
            return "error", "mutagen could not open file", False
        if not isinstance(audio, OggVorbis):
            return "skipped_not_ogg", None, False

        tags = audio.tags
        if tags is None:
            # add_tags() is the public way to create the Vorbis comment header;
            # mutagen 1.47 does not export VComment from mutagen.oggvorbis.
            audio.add_tags()
            tags = audio.tags

        current = None
        if "title" in tags:
            val = tags["title"]
            current = val[0] if isinstance(val, list) else val
            current = str(current).strip() if current is not None else ""

        if current:
            return "skipped_has_title", None, False

        new_title = title_from_filename(path)
        if not new_title:
            return "skipped_empty_name", None, False

        backed_up = False
        if not args.dry_run:
            if args.backup:
                backed_up = backup_binary_file(path)
            tags["title"] = new_title
            audio.save()

        return "written", new_title, backed_up
    except Exception as e:  # noqa: BLE001 - per-file isolation is the point
        return "error", str(e), False


def step_ogg(args: argparse.Namespace, root: Path) -> StepResult:
    files = sorted(p for p in root.rglob("*.ogg") if p.is_file())
    print(f"  Found {len(files)} .ogg files under {root}")

    result = StepResult(
        "ogg",
        counts={
            "written": 0,
            "skipped_has_title": 0,
            "skipped_empty_name": 0,
            "skipped_not_ogg": 0,
            "error": 0,
        },
    )
    changed: list[str] = []

    for i, path in enumerate(files, 1):
        status, detail, backed_up = process_ogg(path, args)
        result.counts[status] = result.counts.get(status, 0) + 1
        if status == "error" and detail:
            result.errors.append(f"{path}: {detail}")
        elif status == "written":
            changed.append(f"{path}  ->  {detail}")
            if backed_up:
                result.notes.append(f"backup: {path.name}.bak")
        if i % 50 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

    report_summary(
        result.counts,
        [
            ("written", "Wrote title:"),
            ("skipped_has_title", "Skipped (already has title):"),
            ("skipped_empty_name", "Skipped (empty filename):"),
            ("skipped_not_ogg", "Skipped (not ogg):"),
            ("error", "Errors:"),
        ],
    )
    report = write_report(
        args.report_dir,
        "ogg_titles_report.txt",
        "These OGG files had no title tag and were given one from the file name.",
        changed,
        args.dry_run,
    )
    if report:
        result.notes.append(f"report: {report}")
    return finish(result)


# ---------------------------------------------------------------------------
# Step 2: extract embedded lyrics into .lrc sidecars
# ---------------------------------------------------------------------------


def parse_embedded_lyrics(text: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Extract timed lines and header metadata from embedded lyric text."""
    if text.startswith("\ufeff"):
        text = text[1:]

    timed: list[tuple[str, str]] = []
    meta: dict[str, str] = {}

    for line in text.splitlines():
        s = line.strip()
        # rmpc only reads metadata before the first timestamp, but keeping
        # these as fallback is harmless.
        m = re.match(r"\[(ti|ar|al|au|length|offset):(.*)\]$", s)
        if m and not timed:
            meta[m.group(1)] = m.group(2).strip()
            continue
        timed.extend(parse_lrc_line(s))

    return timed, meta


def extract_mp3_lyrics(path: Path) -> tuple[str | None, str | None]:
    try:
        id3 = ID3(path)
    except Exception:  # noqa: BLE001
        return None, None

    # Synced lyrics (SYLT) are the most reliable source.
    for frame in id3.getall("SYLT"):
        if getattr(frame, "format", None) == 2 and frame.text:
            pairs = sorted(frame.text, key=lambda pair: pair[1])
            lines = []
            for txt, ms in pairs:
                total_seconds = ms / 1000.0
                minutes = int(total_seconds // 60)
                seconds = total_seconds - minutes * 60
                lines.append(f"[{minutes:02d}:{seconds:05.2f}]{txt}")
            return "\n".join(lines), "synced"

    # Unsynced lyrics (USLT) may still be a full LRC blob.
    uslt = id3.getall("USLT")
    if uslt and uslt[0].text:
        return uslt[0].text, "text"

    return None, None


def extract_vorbis_lyrics(audio) -> tuple[str | None, str | None]:
    for key in ("LYRICS", "SYNCEDLYRICS", "UNSYNCEDLYRICS", "LYRIC"):
        if key in audio:
            val = audio[key]
            return (val[0] if isinstance(val, list) else val), "text"
    return None, None


def extract_mp4_lyrics(audio) -> tuple[str | None, str | None]:
    key = "\xa9lyr"
    if audio.tags and key in audio.tags:
        val = audio.tags[key]
        return (val[0] if isinstance(val, list) else val), "text"
    return None, None


def get_metadata(path: Path) -> tuple[str | None, str | None, str | None, float | None]:
    try:
        easy = MutagenFile(path, easy=True)
    except Exception:  # noqa: BLE001
        easy = None

    artist = title = album = None
    length = None
    if easy is not None:
        if easy.get("artist"):
            artist = easy["artist"][0]
        if easy.get("title"):
            title = easy["title"][0]
        if easy.get("album"):
            album = easy["album"][0]
        if getattr(easy, "info", None) is not None:
            length = getattr(easy.info, "length", None)
    return artist, title, album, length


def sec_to_lrc_len(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def build_lrc(
    timed: list[tuple[str, str]],
    artist: str | None,
    title: str | None,
    album: str | None,
    length: float | None,
    embedded_meta: dict[str, str],
) -> str | None:
    if not timed:
        return None

    header: list[str] = []
    # Prefer real audio metadata; fall back to embedded LRC header tags.
    if title:
        header.append(f"[ti:{title}]")
    elif embedded_meta.get("ti"):
        header.append(f"[ti:{embedded_meta['ti']}]")

    if artist:
        header.append(f"[ar:{artist}]")
    elif embedded_meta.get("ar"):
        header.append(f"[ar:{embedded_meta['ar']}]")

    if album:
        header.append(f"[al:{album}]")
    elif embedded_meta.get("al"):
        header.append(f"[al:{embedded_meta['al']}]")

    if length:
        header.append(f"[length:{sec_to_lrc_len(length)}]")
    elif embedded_meta.get("length"):
        header.append(f"[length:{embedded_meta['length']}]")

    if embedded_meta.get("au"):
        header.append(f"[au:{embedded_meta['au']}]")
    if embedded_meta.get("offset"):
        header.append(f"[offset:{embedded_meta['offset']}]")

    body = [f"{ts}{content}" for ts, content in timed]
    return "\n".join([*header, *body]) + "\n"


def process_extract(path: Path, args: argparse.Namespace) -> tuple[str, str | None]:
    lrc_path = path.with_suffix(".lrc")
    if lrc_path.exists() and not args.overwrite:
        return "skipped_exists", str(lrc_path)

    ext = path.suffix.lower()
    text = None
    try:
        if ext == ".mp3":
            text, _kind = extract_mp3_lyrics(path)
        elif ext in {".flac", ".ogg", ".oga"}:
            audio = MutagenFile(path)
            if audio is not None:
                text, _kind = extract_vorbis_lyrics(audio)
        elif ext in {".m4a", ".mp4", ".m4b"}:
            audio = MutagenFile(path)
            if audio is not None:
                text, _kind = extract_mp4_lyrics(audio)
    except Exception as e:  # noqa: BLE001
        return "error", str(e)

    if not text:
        return "no_lyrics", None

    timed, embedded_meta = parse_embedded_lyrics(text)
    if not timed:
        return "unsynced_skipped", None

    artist, title, album, length = get_metadata(path)
    lrc_content = build_lrc(timed, artist, title, album, length, embedded_meta)
    if lrc_content is None:
        return "unsynced_skipped", None

    if not args.dry_run:
        # A write failure is this file's problem only; the step reports it and
        # carries on with the rest of the library.
        try:
            if args.backup and lrc_path.exists():
                original = lrc_path.read_text(encoding="utf-8", errors="replace")
                backup_text_file(lrc_path, original)
            lrc_path.write_text(lrc_content, encoding="utf-8")
        except OSError as e:
            return "error", str(e)

    return "written", str(lrc_path)


def step_extract(args: argparse.Namespace, root: Path) -> StepResult:
    files = audio_files(root)
    print(f"  Found {len(files)} audio files under {root}")

    result = StepResult(
        "extract",
        counts={
            "written": 0,
            "skipped_exists": 0,
            "no_lyrics": 0,
            "unsynced_skipped": 0,
            "error": 0,
        },
    )
    unsynced: list[str] = []
    missing: list[str] = []

    for i, path in enumerate(files, 1):
        status, detail = process_extract(path, args)
        result.counts[status] = result.counts.get(status, 0) + 1
        if status == "unsynced_skipped":
            unsynced.append(str(path))
        elif status == "no_lyrics":
            missing.append(str(path))
        elif status == "error":
            result.errors.append(f"{path}: {detail}")
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

    report_summary(
        result.counts,
        [
            ("written", "LRC written:"),
            ("skipped_exists", "Skipped (already has LRC):"),
            ("no_lyrics", "No embedded lyrics found:"),
            ("unsynced_skipped", "Unsynced lyrics skipped:"),
            ("error", "Errors:"),
        ],
    )

    for filename, header, items in (
        (
            "lrc_unsynced_report.txt",
            "These files have embedded lyrics but no valid timed LRC lines.",
            unsynced,
        ),
        (
            "lrc_missing_report.txt",
            "These files have no embedded lyrics tag at all.",
            missing,
        ),
        ("lrc_error_report.txt", "Errors while extracting lyrics.", result.errors),
    ):
        report = write_report(args.report_dir, filename, header, items, args.dry_run)
        if report:
            result.notes.append(f"report: {report}")
    return finish(result)


# ---------------------------------------------------------------------------
# Step: move author/credit info out of .lrc and into the audio file's tags
# ---------------------------------------------------------------------------

# Normalized role (lowercase, all whitespace removed) -> canonical field name.
# Curated on purpose: lyric lines that merely contain a colon ("她说：...",
# "但我们最终归航：...") must never be mistaken for credits, so only these
# exact role spellings are accepted.
CREDIT_ROLES: dict[str, str] = {
    # lyricist
    "作词": "lyricist", "作詞": "lyricist", "词": "lyricist", "詞": "lyricist",
    "lyricist": "lyricist", "lyrics": "lyricist", "lyricsby": "lyricist",
    "shi": "lyricist",  # romaji reading of 詞, used by some LRC sources
    # composer
    "作曲": "composer", "曲": "composer", "composer": "composer",
    "composedby": "composer", "musicby": "composer", "music": "composer",
    "kyoku": "composer",  # romaji reading of 曲
    # arranger
    "编曲": "arranger", "編曲": "arranger", "arranger": "arranger",
    "arrangedby": "arranger", "管乐编写": "arranger",
    # producer
    "制作人": "producer", "制作": "producer", "producer": "producer",
    "producedby": "producer", "p主": "producer",
    # mixer / mastering / engineer
    "混音": "mixer", "mixer": "mixer", "mixedby": "mixer",
    "母带": "mastering", "masteredby": "mastering",
    "录音": "engineer", "engineer": "engineer", "recordedby": "engineer",
    "人声录音": "engineer", "器乐录音": "engineer",
    "音频编辑": "engineer", "人声工程师": "engineer",
    # combined writing credit
    "writtenby": "writer",
    # performers
    "人声": "performer", "主唱": "performer", "演唱": "performer", "唱": "performer",
    "歌手": "performer", "和声": "performer", "vocal": "performer",
    "vocalist": "performer", "singer": "performer",
    "原唱": "original_vocalist", "翻唱": "cover_vocalist",
    # remaining credits
    "译": "translator", "翻译": "translator",
    "来源": "source", "翻译取自": "source",
    "监制": "supervisor", "轴": "subtitle_timing",
    "封面": "cover_art", "illustration": "cover_art",
}

# Normalized Chinese instrument role -> English instrument name.
INSTRUMENT_ROLES: dict[str, str] = {
    "吉他": "guitar", "木吉他": "acoustic guitar", "电吉他": "electric guitar",
    "12弦木吉他": "12-string guitar", "莱雅琴": "lyre",
    "贝斯": "bass", "提琴贝斯": "double bass", "doublebass": "double bass",
    "鼓": "drums", "打击乐": "percussion",
    "钢琴": "piano", "键盘": "keyboard",
    "小提琴": "violin", "中提琴": "viola", "大提琴": "cello",
    "长笛": "flute", "低音单簧管": "bass clarinet",
    "萨克斯": "saxophone", "中音萨克斯": "alto saxophone",
    "次中音萨克斯": "tenor saxophone", "上低音萨克斯": "baritone saxophone",
    "小号": "trumpet", "长号": "trombone", "次中音号": "euphonium",
    "唢呐": "suona", "笛子": "dizi", "古筝": "guzheng", "阮": "ruan",
    "采样": "sampling", "硬件噪音": "hardware noise",
}

# Canonical field -> standard ID3v2.4 text frame.
ID3_TEXT_FIELDS = {"lyricist": "TEXT", "composer": "TCOM"}
# Canonical field -> TIPL (involved people list) role.
ID3_TIPL_FIELDS = {
    "arranger": "arranger", "engineer": "engineer",
    "producer": "producer", "mixer": "mix",
}

CREDIT_ROLE_SPLIT = re.compile(r"[/、,，;；]")
PERFORMER_PREFIX = "performer:"
CREDIT_SEPARATORS = ("：", ":")

# LRC header tags dropped because they carry no lyric content and are not
# needed to match a track to its lyrics: source-site ids, encoding hints and
# empty stub fields.
#
# ti / ar / al are deliberately KEPT: rmpc indexes lyrics by them and picks an
# entry from that index, and `length` drives its "closest match by length"
# tie-break.  Dropping them would degrade lyric matching to filename-only.
DROPPABLE_LRC_TAGS = {"by", "kuwo", "ver", "hash", "sign", "qq", "total", "kana"}

# [name:value] header tag, and a bracketed prefix of any shape (which also
# catches credit lines behind a malformed timestamp such as "[00:-1.0]").
LRC_META_RE = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_]*):([^\]]*)\]$")
BRACKETED_RE = re.compile(r"^\[([^\]]*)\](.*)$")


def normalize_role(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s).lower()


def credit_field_for_role(role: str) -> str | None:
    if role in CREDIT_ROLES:
        return CREDIT_ROLES[role]
    if role in INSTRUMENT_ROLES:
        return PERFORMER_PREFIX + INSTRUMENT_ROLES[role]
    return None


def parse_credit_line(content: str) -> tuple[list[str], str] | None:
    """Return (fields, value) when a lyric line carries author/credit info.

    Deliberately conservative: if any part of a compound role is unrecognized
    the whole line is rejected, so real lyrics containing a colon are kept.
    """
    role_part = value = None
    for sep in CREDIT_SEPARATORS:
        if sep in content:
            role_part, value = content.split(sep, 1)
            break
    if role_part is None or value is None:
        return None

    value = value.strip()
    if not value or value in {"//", "/"}:
        return None

    fields: list[str] = []
    for raw in CREDIT_ROLE_SPLIT.split(role_part):
        field = credit_field_for_role(normalize_role(raw))
        if field is None:
            return None
        fields.append(field)
    return (fields, value) if fields else None


def _norm_cmp(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s).lower()


def looks_like_title_line(content: str, artist_names: list[str]) -> bool:
    """True for a leading "<title> - <artist>" line that duplicates [ti:]/[ar:]."""
    if " - " not in content or len(content) > 200:
        return False
    tail = _norm_cmp(content.split(" - ", 1)[1])
    if not tail:
        return False
    return any(
        len(a) >= 2 and a in tail for a in (_norm_cmp(n) for n in artist_names if n)
    )


# --- tag writers -----------------------------------------------------------


def _write_vorbis_credits(path: Path, credits: dict[str, str]) -> list[str]:
    audio = MutagenFile(path)
    if audio is None:
        raise OSError("mutagen could not open file")
    changes: list[str] = []
    for field, value in credits.items():
        old = audio.get(field)
        old_s = " / ".join(str(x) for x in old) if isinstance(old, list) and old else ""
        if old_s == value:
            changes.append(f"    [=] {field} = {value}")
            continue
        audio[field] = [value]
        changes.append(f"    [{'~' if old_s else '+'}] {field} = {old_s or '(empty)'} -> {value}")
    audio.save()
    return changes


def _id3_existing_credits(tags) -> dict[str, str]:
    """Best-effort read of the credit fields this step manages."""
    out: dict[str, str] = {}

    def add(field: str, value: str) -> None:
        out[field] = f"{out[field]} / {value}" if field in out else value

    for field, frame_id in ID3_TEXT_FIELDS.items():
        frame = tags.get(frame_id)
        if frame is not None and getattr(frame, "text", None):
            out[field] = " / ".join(str(t) for t in frame.text)

    # People live in TIPL + TMCL for ID3v2.4, and in a single IPLS frame for
    # ID3v2.3 (which mutagen reads back as TIPL).  Musician roles are read as
    # performer:<role> so they are not silently dropped from the diff.
    for role, people in _people_map(tags, "TIPL", "IPLS").items():
        field = next((f for f, r in ID3_TIPL_FIELDS.items() if r == role), None)
        if field is None:
            field = "performer" if role == "vocals" else PERFORMER_PREFIX + role
        for person in people:
            add(field, person)

    for role, people in _people_map(tags, "TMCL").items():
        field = "performer" if role == "vocals" else PERFORMER_PREFIX + role
        for person in people:
            add(field, person)

    for frame in tags.getall("TXXX"):
        desc = str(getattr(frame, "desc", "")).lower()
        if desc and getattr(frame, "text", None):
            out[desc] = " / ".join(str(t) for t in frame.text)
    return out


def _people_map(tags, *frame_ids: str) -> dict[str, list[str]]:
    """Collect (role -> people) from the first frame that exists."""
    out: dict[str, list[str]] = {}
    for frame_id in frame_ids:
        frame = tags.get(frame_id)
        if frame is None:
            continue
        for role, person in getattr(frame, "people", []):
            out.setdefault(role, []).append(person)
    return out


def _flatten_people(people: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(role, person) for role, persons in people.items() for person in persons]


def _write_id3_credits(path: Path, credits: dict[str, str]) -> list[str]:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    before = _id3_existing_credits(tags)
    # Keep the file's existing ID3 version: TIPL/TMCL are ID3v2.4 frames, while
    # ID3v2.3 carries the same information in a single IPLS frame.  Upgrading
    # 2.3 files to 2.4 would rewrite every one of them for no benefit.
    version = tags.version or (2, 4, 0)
    v2_version = 4 if version[1] >= 4 else 3

    # TEXT (lyricist) / TCOM (composer)
    for field, frame_id in ID3_TEXT_FIELDS.items():
        if field in credits:
            frame_cls = TEXT if frame_id == "TEXT" else TCOM
            tags.setall(frame_id, [frame_cls(encoding=3, text=[credits[field]])])

    if v2_version >= 4:
        # Involved people and musicians live in separate frames.
        tipl_people = _people_map(tags, "TIPL")
        for field, role in ID3_TIPL_FIELDS.items():
            if field in credits:
                tipl_people[role] = [credits[field]]

        tmcl_people = _people_map(tags, "TMCL")
        for field, value in credits.items():
            if field.startswith(PERFORMER_PREFIX):
                tmcl_people[field[len(PERFORMER_PREFIX):]] = [value]
        if "performer" in credits:
            tmcl_people["vocals"] = [credits["performer"]]

        # mutagen surfaces an ID3v2.3 IPLS frame as TIPL.  Leaving both on
        # disk makes the reader pick one and silently drop the other, so clear
        # both spellings before writing the merged result.
        tags.delall("TIPL")
        tags.delall("IPLS")
        if tipl_people:
            tags.setall("TIPL", [TIPL(encoding=3, people=_flatten_people(tipl_people))])
        if tmcl_people:
            tags.setall("TMCL", [TMCL(encoding=3, people=_flatten_people(tmcl_people))])
    else:
        # ID3v2.3: one IPLS frame carries both groups.  mutagen writes the
        # literal "IPLS" frame id when v2_version=3 and maps it back to TIPL
        # on read, so _id3_existing_credits() sees it either way.
        ipls_people = _people_map(tags, "IPLS", "TIPL")
        for field, role in ID3_TIPL_FIELDS.items():
            if field in credits:
                ipls_people[role] = [credits[field]]
        for field, value in credits.items():
            if field.startswith(PERFORMER_PREFIX):
                ipls_people[field[len(PERFORMER_PREFIX):]] = [value]
        if "performer" in credits:
            ipls_people["vocals"] = [credits["performer"]]
        # See above: the pre-existing frame is keyed "TIPL" in memory even
        # though it is stored as IPLS, so both must be cleared.
        tags.delall("TIPL")
        tags.delall("IPLS")
        if ipls_people:
            tags.setall("IPLS", [IPLS(encoding=3, people=_flatten_people(ipls_people))])

    # Everything else goes to a TXXX frame named after the field.
    for field, value in credits.items():
        if field in ID3_TEXT_FIELDS or field in ID3_TIPL_FIELDS:
            continue
        if field == "performer" or field.startswith(PERFORMER_PREFIX):
            continue
        desc = field.upper()
        tags.setall(f"TXXX:{desc}", [TXXX(encoding=3, desc=desc, text=[value])])

    tags.save(path, v2_version=v2_version)

    after = _id3_existing_credits(tags)
    changes: list[str] = []
    for field in credits:
        old_s = before.get(field, before.get(field.upper(), ""))
        new_s = after.get(field, after.get(field.upper(), credits[field]))
        mark = "=" if old_s == new_s else ("~" if old_s else "+")
        changes.append(f"    [{mark}] {field} = {old_s or '(empty)'} -> {new_s}")
    return changes


def _write_mp4_credits(path: Path, credits: dict[str, str]) -> list[str]:
    audio = MutagenFile(path)
    if audio is None:
        raise OSError("mutagen could not open file")
    changes: list[str] = []
    for field, value in credits.items():
        key = f"----:com.apple.iTunes:{field.upper().replace(':', '_')}"
        old = audio.tags.get(key) if audio.tags else None
        old_s = old[0].decode("utf-8", "replace") if old else ""
        audio[key] = [value.encode("utf-8")]
        changes.append(f"    [{'~' if old_s else '+'}] {field} = {old_s or '(empty)'} -> {value}")
    audio.save()
    return changes


def write_credits_to_audio(path: Path, credits: dict[str, str]) -> list[str]:
    """Write credits into the audio file's tags; returns a change log."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        return _write_id3_credits(path, credits)
    if ext in {".flac", ".ogg", ".oga"}:
        return _write_vorbis_credits(path, credits)
    if ext in {".m4a", ".mp4", ".m4b"}:
        return _write_mp4_credits(path, credits)
    raise OSError(f"unsupported audio format: {ext}")


# --- the step --------------------------------------------------------------


def find_audio_for_lrc(lrc: Path) -> Path | None:
    """Return the sibling audio file sharing this .lrc's stem."""
    for ext in sorted(AUDIO_EXTS):
        candidate = lrc.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


@dataclass
class CreditPlan:
    """What would be moved out of one .lrc file."""

    credits: dict[str, list[str]] = field(default_factory=dict)
    remove_idx: set[int] = field(default_factory=set)
    removed_preview: list[str] = field(default_factory=list)

    def resolved(self) -> dict[str, str]:
        """Collapse accumulated values per field, de-duplicated and joined."""
        out: dict[str, str] = {}
        for field_name, values in self.credits.items():
            seen = list(dict.fromkeys(v for v in values if v))
            if seen:
                out[field_name] = " / ".join(seen)
        return out


def plan_credit_extraction(lines: list[str], artist_names: list[str]) -> CreditPlan:
    """Decide which lines carry author info or non-lyric metadata.

    Runs over the whole file, not just the timed body: credits can hide in the
    header region behind a malformed timestamp ("[00:-1.0]作词: X"), which a
    body-only scan never sees because TIMESTAMP_RE cannot match it.
    """
    plan = CreditPlan()
    # Title lines only appear in the leading block, and a source may carry
    # several of them (original plus a romaji transliteration), so eligibility
    # lasts until the first genuine lyric line rather than only index 0.
    in_leading_block = True

    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue

        m = TIMESTAMP_RE.match(s)
        if not m:
            # Header region: either a droppable metadata tag, or a credit line
            # whose timestamp is malformed and so never matched above.
            meta = LRC_META_RE.match(s)
            if meta:
                tag, value = meta.group(1).lower(), meta.group(2).strip()
                if tag in DROPPABLE_LRC_TAGS or (tag == "offset" and value in ("", "0")):
                    plan.removed_preview.append(f"{s}   (metadata tag)")
                    plan.remove_idx.add(idx)
                continue

            bracketed = BRACKETED_RE.match(s)
            if bracketed:
                rest = bracketed.group(2).strip()
                parsed = parse_credit_line(rest) if rest else None
                if parsed is not None:
                    fields, value = parsed
                    for f in fields:
                        plan.credits.setdefault(f, []).append(value)
                    plan.removed_preview.append(
                        f"{s}   -> {', '.join(fields)} (malformed timestamp)"
                    )
                    plan.remove_idx.add(idx)
            continue

        content = s[m.end():].strip()

        # "//" is a separator some sources emit between label lines.  It is
        # never lyrics, so it always goes.
        if content in ("//", "/"):
            plan.removed_preview.append(f"{s}   (separator)")
            plan.remove_idx.add(idx)
            continue

        parsed = parse_credit_line(content)
        is_title = (
            in_leading_block
            and m.group(1).startswith("00:00")
            and looks_like_title_line(content, artist_names)
        )
        if parsed is None and not is_title:
            in_leading_block = False  # first real lyric ends the leading block
            continue

        if is_title:
            plan.removed_preview.append(f"{s}   (title line)")
        else:
            fields, value = parsed  # type: ignore[misc]
            for f in fields:
                plan.credits.setdefault(f, []).append(value)
            plan.removed_preview.append(f"{s}   -> {', '.join(fields)}")
        plan.remove_idx.add(idx)

    return plan


def step_credits(args: argparse.Namespace, root: Path) -> StepResult:
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")

    result = StepResult(
        "credits",
        counts={
            "moved": 0,
            "skipped_no_credits": 0,
            "skipped_no_audio": 0,
            "error": 0,
        },
    )
    move_log: list[str] = []

    for i, lrc in enumerate(files, 1):
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

        audio = find_audio_for_lrc(lrc)
        if audio is None:
            result.counts["skipped_no_audio"] += 1
            result.notes.append(f"no audio for: {lrc.name}")
            continue

        try:
            text = lrc.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{lrc}: {e}")
            continue

        # Drop a BOM so the first header tag is recognised like any other.
        if text.startswith("\ufeff"):
            text = text[1:]
        lines = text.splitlines()

        audio_meta = get_metadata(audio)
        artist_tag = audio_meta[0]
        artist_names = [a.strip() for a in re.split(r"[;,/]", artist_tag)] if artist_tag else []
        for line in lines:
            m = re.match(r"\[ar:(.*)\]$", line.strip())
            if m:
                artist_names.extend(a.strip() for a in re.split(r"[;,/、]", m.group(1)))

        plan = plan_credit_extraction(lines, artist_names)
        resolved = plan.resolved()
        if not resolved and not plan.remove_idx:
            result.counts["skipped_no_credits"] += 1
            continue

        if args.dry_run:
            print(f"    WOULD MOVE: {lrc.name}")
            for line in plan.removed_preview[:DRY_RUN_PREVIEW_LINES]:
                print(f"        - {line}")
            extra = len(plan.removed_preview) - DRY_RUN_PREVIEW_LINES
            if extra > 0:
                print(f"        ... (+{extra} more lines)")
            result.counts["moved"] += 1
            continue

        # Write tags FIRST; only strip the .lrc once that succeeded.
        if resolved:
            try:
                change_lines = write_credits_to_audio(audio, resolved)
            except Exception as e:  # noqa: BLE001 - never lose lyrics on failure
                result.counts["error"] += 1
                result.errors.append(f"{audio.name}: tag write failed ({e}); .lrc untouched")
                continue
            move_log.append(f"{lrc.name}  ->  {audio.name}")
            move_log.extend(change_lines)

        if plan.remove_idx:
            kept = [ln for idx, ln in enumerate(lines) if idx not in plan.remove_idx]
            new_text = "\n".join(kept)
            if text.endswith("\n"):
                new_text += "\n"
            # The tags are already written, so a failure here only means the
            # credit lines stay in the .lrc (duplicated, nothing lost).
            try:
                if args.backup:
                    backup_text_file(lrc, text)
                lrc.write_text(new_text, encoding="utf-8")
            except OSError as e:
                result.counts["error"] += 1
                result.errors.append(f"{lrc}: cannot write: {e}")
                continue

        result.counts["moved"] += 1

    report_summary(
        result.counts,
        [
            ("moved", "Files processed:"),
            ("skipped_no_credits", "Skipped (no credits found):"),
            ("skipped_no_audio", "Skipped (no matching audio):"),
            ("error", "Errors:"),
        ],
    )
    if move_log:
        report = write_report(
            args.report_dir,
            "credits_moved_report.txt",
            "Author info moved from .lrc into audio tags. '[+]' added, "
            "'[~]' overwritten, '[=]' unchanged; 'old -> new' shows what was replaced.",
            move_log,
            args.dry_run,
        )
        if report:
            result.notes.append(f"report: {report}")
    return finish(result)


# ---------------------------------------------------------------------------
# Step 3: normalize songs without real lyrics
# ---------------------------------------------------------------------------


def normalize_text(s: str) -> str:
    """Remove spaces/punctuation and lower-case for placeholder detection."""
    return re.sub(r"[\s\u3000]+", "", s).lower()


def is_placeholder(content: str) -> bool:
    norm = normalize_text(content)
    if not norm:
        return True  # empty lines are not real lyrics
    if norm in {"//", "/"}:
        return True
    # Placeholder phrases can appear anywhere in the line.
    if any(kw in norm for kw in PLACEHOLDER_KEYWORDS):
        return True
    # Credit/metadata lines only count as non-lyrics at the start of the line,
    # so words like "吉他" inside real lyrics do not match.
    if any(norm.startswith(kw) for kw in NON_LYRIC_KEYWORDS):
        return True
    return False


def is_metadata_line(line: str) -> bool:
    """True for lines like [ti:...], [ar:...], [by:...], [offset:...]."""
    s = line.strip()
    return bool(re.match(r"^\[[^\[\]]+\][^\[\]]*$", s)) and ":" in s.split("]", 1)[0]


def has_real_lyrics_in_text(text: str) -> bool:
    """Return True if the text contains any real lyric line.

    Handles both timed LRC lines and plain/untimed lyric text.
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        timed = parse_lrc_line(line)
        if timed:
            if any(not is_placeholder(content) for _, content in timed):
                return True
            continue

        # Untimed line: skip metadata tags and keep real lyric text.
        if line.startswith("["):
            if is_metadata_line(line):
                continue
            # e.g. "[Intro]" or "[Chorus]" could be part of lyrics; treat as
            # real if not a placeholder.
            if not is_placeholder(line):
                return True
            continue

        if not is_placeholder(line):
            return True

    return False


def existing_lrc_header(text: str) -> dict[str, str]:
    """Read the [ti:]/[ar:]/[al:] values already present in a .lrc file."""
    if text.startswith("\ufeff"):
        text = text[1:]
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = LRC_META_RE.match(line.strip())
        if m and m.group(1).lower() in {"ti", "ar", "al"}:
            value = m.group(2).strip()
            if value:
                out.setdefault(m.group(1).lower(), value)
    return out


def build_no_lyrics_lrc(
    artist: str | None,
    title: str | None,
    album: str | None,
    length: float | None,
    fallback: dict[str, str] | None = None,
) -> str:
    # Audio metadata wins; a header the .lrc already carried is kept rather than
    # thrown away when the audio file has no such tag.
    fallback = fallback or {}
    title = title or fallback.get("ti")
    artist = artist or fallback.get("ar")
    album = album or fallback.get("al")

    header: list[str] = []
    if title:
        header.append(f"[ti:{title}]")
    if artist:
        header.append(f"[ar:{artist}]")
    if album:
        header.append(f"[al:{album}]")
    if length:
        header.append(f"[length:{sec_to_lrc_len(length)}]")
    header.append("[00:00.00]No lyrics")
    return "\n".join(header) + "\n"


def extract_embedded_text(path: Path) -> str | None:
    """Return embedded lyric text if present, otherwise None."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            id3 = ID3(path)
            for frame in id3.getall("SYLT"):
                if getattr(frame, "format", None) == 2 and frame.text:
                    lines = []
                    for txt, ms in sorted(frame.text, key=lambda p: p[1]):
                        total_seconds = ms / 1000.0
                        minutes = int(total_seconds // 60)
                        seconds = total_seconds - minutes * 60
                        lines.append(f"[{minutes:02d}:{seconds:05.2f}]{txt}")
                    if lines:
                        return "\n".join(lines)
            uslt = id3.getall("USLT")
            if uslt and uslt[0].text:
                return uslt[0].text
        elif ext in {".flac", ".ogg", ".oga", ".m4a", ".mp4", ".m4b"}:
            audio = MutagenFile(path)
            if audio is None:
                return None
            if ext in {".m4a", ".mp4", ".m4b"}:
                key = "\xa9lyr"
                if audio.tags and key in audio.tags:
                    val = audio.tags[key]
                    return val[0] if isinstance(val, list) else val
            else:
                for key in ("LYRICS", "SYNCEDLYRICS", "UNSYNCEDLYRICS", "LYRIC"):
                    if key in audio:
                        val = audio[key]
                        return val[0] if isinstance(val, list) else val
    except Exception:  # noqa: BLE001
        return None
    return None


def process_no_lyrics(path: Path, args: argparse.Namespace) -> str:
    lrc_path = path.with_suffix(".lrc")

    if lrc_path.exists():
        try:
            existing = lrc_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"error: {e}"

        if has_real_lyrics_in_text(existing):
            return "skipped_has_lyrics"

        # No real lyrics -> rewrite uniformly.
        artist, title, album, length = get_metadata(path)
        new_content = build_no_lyrics_lrc(
            artist, title, album, length, existing_lrc_header(existing)
        )
        if existing == new_content:
            return "skipped_no_change"
        if not args.dry_run:
            try:
                if args.backup:
                    backup_text_file(lrc_path, existing)
                lrc_path.write_text(new_content, encoding="utf-8")
            except OSError as e:
                return f"error: {e}"
        return "written"

    # No .lrc file.  Check embedded lyrics so real lyrics are not marked none.
    embedded = extract_embedded_text(path)
    if embedded and has_real_lyrics_in_text(embedded):
        return "skipped_has_lyrics"

    artist, title, album, length = get_metadata(path)
    new_content = build_no_lyrics_lrc(artist, title, album, length)
    if not args.dry_run:
        try:
            lrc_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"error: {e}"
    return "written"


def step_no_lyrics(args: argparse.Namespace, root: Path) -> StepResult:
    files = audio_files(root)
    print(f"  Found {len(files)} audio files under {root}")

    result = StepResult(
        "nolyrics",
        counts={
            "written": 0,
            "skipped_has_lyrics": 0,
            "skipped_no_change": 0,
            "error": 0,
        },
    )

    for i, path in enumerate(files, 1):
        status = process_no_lyrics(path, args)
        if status.startswith("error"):
            result.counts["error"] += 1
            result.errors.append(f"{path}: {status[7:]}")
        else:
            result.counts[status] = result.counts.get(status, 0) + 1
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

    report_summary(
        result.counts,
        [
            ("written", "Wrote 'No lyrics':"),
            ("skipped_has_lyrics", "Skipped (has real lyrics):"),
            ("skipped_no_change", "Skipped (already correct):"),
            ("error", "Errors:"),
        ],
    )
    return finish(result)


# ---------------------------------------------------------------------------
# Step 4: group same-timestamp lines consecutively (bilingual layout)
# ---------------------------------------------------------------------------


def bilingual_process_lrc(text: str, max_lines: int | None) -> tuple[str | None, int]:
    """Return (regrouped text or None, lines removed by max_lines).

    ``None`` means nothing needs to change, so the file is left alone.
    """
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text[1:]

    split = split_header_and_body(text)
    if split is None:
        return None, 0  # no timed lyrics, leave untouched
    header, body = split

    new_body, _reordered, dropped = regroup_body_lines(
        body,
        raw_timed_entries,
        # Use the first timestamp's spelling for the whole group, padding only
        # a bare [MM:SS] to [MM:SS.00].
        lambda group: f"[{pad_bare_timestamp(group[0][0])}]",
        max_lines=max_lines,
    )
    if not new_body:
        return None, dropped

    new_text = render(header, new_body, text.endswith("\n"))
    if new_text == text:
        # If the only change was removing the BOM, still report it as changed.
        return (new_text, dropped) if had_bom else (None, dropped)
    return new_text, dropped


def has_repeated_timestamp(text: str) -> bool:
    """Quick check: does any timestamp appear more than once?"""
    seen: set[int] = set()
    for line in text.splitlines():
        for ts, _content in raw_timed_entries(line):
            ms = timestamp_to_ms(ts)
            if ms is None:
                continue
            if ms in seen:
                return True
            seen.add(ms)
    return False


def step_bilingual(args: argparse.Namespace, root: Path) -> StepResult:
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")

    result = StepResult(
        "bilingual", counts={"written": 0, "skipped": 0, "truncated": 0, "error": 0}
    )

    for path in files:
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{path}: cannot read: {e}")
            continue

        if args.only_duplicates and not has_repeated_timestamp(original):
            result.counts["skipped"] += 1
            continue

        new_text, dropped = bilingual_process_lrc(original, max_lines=args.max_lines)
        result.counts["truncated"] += dropped
        if new_text is None:
            result.counts["skipped"] += 1
            continue

        if args.dry_run:
            print(f"    WOULD UPDATE: {path}")
            result.counts["written"] += 1
            continue

        # One unwritable file must not abort the remaining files or steps.
        try:
            if args.backup:
                backup_text_file(path, original)
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{path}: cannot write: {e}")
            continue
        result.counts["written"] += 1

    report_summary(
        result.counts,
        [
            ("written", "Updated:"),
            ("skipped", "Skipped/unchanged:"),
            ("truncated", "Lines dropped by --max-lines:"),
            ("error", "Errors:"),
        ],
    )
    if result.counts["truncated"]:
        result.notes.append(
            "note: --max-lines dropped lyric lines; those lines are gone from the .lrc "
            "unless you kept a backup"
        )
    return finish(result)


# ---------------------------------------------------------------------------
# Step 5: move romaji between the original and the Chinese line
# ---------------------------------------------------------------------------


def has_kana(s: str) -> bool:
    return bool(re.search(r"[\u3040-\u30FF]", s))


def has_han(s: str) -> bool:
    return bool(re.search(r"[\u4E00-\u9FFF]", s))


def has_roman(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s))


def is_romaji(s: str) -> bool:
    return has_roman(s) and not has_kana(s) and not has_han(s)


def is_chinese(s: str) -> bool:
    return has_han(s) and not has_kana(s)


def reorder_group(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reorder a same-timestamp group if it is [orig, chinese, romaji]."""
    if len(entries) != 3:
        return entries
    first, second, third = entries
    if is_chinese(second[1]) and is_romaji(third[1]):
        return [first, third, second]
    return entries


def romaji_process_lrc(text: str) -> str | None:
    if text.startswith("\ufeff"):
        text = text[1:]

    split = split_header_and_body(text)
    if split is None:
        return None
    header, body = split

    new_body, reordered, _dropped = regroup_body_lines(
        body,
        parse_lrc_line,
        lambda group: group[0][0],  # already a normalized [MM:SS.ff]
        reorder=reorder_group,
    )

    if not reordered:
        return None
    return render(header, new_body, text.endswith("\n"))


def step_romaji(args: argparse.Namespace, root: Path) -> StepResult:
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")

    result = StepResult("romaji", counts={"written": 0, "skipped": 0, "error": 0})

    for path in files:
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{path}: {e}")
            continue

        new_text = romaji_process_lrc(original)
        if new_text is None:
            result.counts["skipped"] += 1
            continue

        if args.dry_run:
            print(f"    WOULD UPDATE: {path}")
            result.counts["written"] += 1
            continue

        # One unwritable file must not abort the remaining files or steps.
        try:
            if args.backup:
                backup_text_file(path, original)
            path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{path}: cannot write: {e}")
            continue
        result.counts["written"] += 1

    report_summary(
        result.counts,
        [("written", "Updated:"), ("skipped", "Skipped/unchanged:"), ("error", "Errors:")],
    )
    return finish(result)


# ---------------------------------------------------------------------------
# MPD refresh
# ---------------------------------------------------------------------------


def refresh_mpd() -> bool:
    """Refresh the MPD database.  Returns False when the refresh did not work."""
    print("\n=====> Refresh MPD database")
    for tool, argv in (("rmpc", ["rmpc", "update"]), ("mpc", ["mpc", "update"])):
        if shutil.which(tool):
            try:
                proc = subprocess.run(argv, check=False)
            except OSError as e:
                print(f"  Warning: {tool} update failed: {e}", file=sys.stderr)
                return False
            if proc.returncode != 0:
                # A stopped MPD (or a refused socket) must not look like success.
                print(
                    f"  Warning: {tool} update exited {proc.returncode}; "
                    "the MPD database was NOT refreshed",
                    file=sys.stderr,
                )
                return False
            return True
    print("  Warning: neither rmpc nor mpc found, please update MPD manually")
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_steps(spec: str) -> list[str]:
    if spec.strip().lower() == "all":
        return list(STEP_ORDER)
    steps: list[str] = []
    for raw in spec.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in STEP_ORDER:
            raise SystemExit(
                f"Unknown step '{name}'. Choose from: all, {', '.join(STEP_ORDER)}"
            )
        if name not in steps:
            steps.append(name)
    if not steps:
        raise SystemExit("No steps selected.")
    # Normalize into pipeline order so 'romaji,extract' still runs correctly.
    return [s for s in STEP_ORDER if s in steps]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "music_dir",
        nargs="?",
        default=DEFAULT_MUSIC_DIR,
        help="Root directory of your music library (default: %(default)s)",
    )
    parser.add_argument(
        "--steps",
        default="all",
        metavar="LIST",
        help="Comma-separated steps to run, or 'all' (default: %(default)s). "
        f"Choices: all, {', '.join(STEP_ORDER)}",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--backup", action="store_true", help="Keep a .bak copy before overwriting anything")
    parser.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="Where review reports are written (default: %(default)s)",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not run rmpc/mpc update after the steps",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="extract: overwrite existing .lrc files",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=None,
        metavar="N",
        help="bilingual: keep at most N lines per timestamp, dropping the rest "
        "(default: keep all)",
    )
    parser.add_argument(
        "--only-duplicates",
        action="store_true",
        help="bilingual: only rewrite files that already have repeated timestamps",
    )
    args = parser.parse_args(argv)

    if args.max_lines is not None and args.max_lines < 1:
        parser.error("--max-lines must be >= 1")
    return args


RUNNERS = {
    "ogg": step_ogg,
    "extract": step_extract,
    "credits": step_credits,
    "nolyrics": step_no_lyrics,
    "bilingual": step_bilingual,
    "romaji": step_romaji,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    steps = resolve_steps(args.steps)

    root = Path(args.music_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2
    args.report_dir = Path(args.report_dir).expanduser().resolve()

    print("=" * 60)
    print(f" Music library: {root}")
    print(f" Steps:         {', '.join(steps)}")
    print(f" Dry-run:       {'yes' if args.dry_run else 'no'}")
    print(f" Backup:        {'yes' if args.backup else 'no'}")
    print(f" Reports:       {args.report_dir}")
    print("=" * 60)

    failures = 0
    aborted_in: str | None = None
    for name in steps:
        print(f"\n=====> [{name}]")
        try:
            result = RUNNERS[name](args, root)
        except Exception as e:  # noqa: BLE001 - a failed step must stop the run
            print(f"  STEP FAILED: {e}", file=sys.stderr)
            failures += 1
            aborted_in = name
            break
        if result.errors:
            failures += 1

    if args.dry_run:
        print("\n=====> Refresh MPD database (skipped in dry-run)")
    elif args.no_refresh:
        print("\n=====> Refresh MPD database (skipped: --no-refresh)")
    elif not refresh_mpd():
        failures += 1

    print("\n" + "=" * 60)
    if aborted_in:
        print(f" Run aborted in step '{aborted_in}'; later steps were not run.")
        print("=" * 60)
        return 1
    if failures:
        print(f" Done with {failures} step(s) reporting errors.")
        print("=" * 60)
        return 1
    print(" Done.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
