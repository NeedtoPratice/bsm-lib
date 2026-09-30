#!/usr/bin/env python3
"""Unified maintenance for an MPD/rmpc music library.

This single script replaces the former set:

    fix_ogg_titles.py + extract_lrc_rmpc.py + normalize_no_lyrics.py
    + bilingual_lrc.py + reorder_romaji.py + one_click_update.sh

The library standard it enforces:

* a ``.lrc`` holds lyrics and nothing but lyrics -- no ``[ti:]``/``[ar:]``/
  ``[al:]``/``[length:]`` header, no author lines, no source-site notice, no
  site metadata blob.  A song without lyrics gets exactly one line,
  ``[00:00.00]No lyrics``;
* every line of a same-timestamp group carries that timestamp, so the original,
  the romaji and the translation of one line stay together;
* the audio file's name is ``<title> - <artist>.<ext>``, taken from its own tags.

Steps, executed in this order by default:

    ogg        fill missing/empty OGG title tags from the file name
    extract    extract embedded timed lyrics into rmpc-compatible .lrc files
    clean      leave nothing but lyrics: move author info into audio tags
               (rules/roles.txt) and drop headers, credits and source-site
               notices (rules/notices.txt)
    nolyrics   write the single "[00:00.00]No lyrics" line when a song has none
    bilingual  give untimed translation/romaji lines their neighbour's
               timestamp, then group same-timestamp lines together
    romaji     move romaji lines between the original and the Chinese line

Each step runs twice in an interactive run: first in **plan** mode, which writes
the full report and changes nothing, then -- only after you confirm it -- in
**apply** mode.  Pressing Enter at the prompt shows the head of that report, so
the decision is made with the whole list in front of you.  Nothing is ever
dropped from a ``.lrc`` without appearing in a report.

``--dry-run`` writes nothing at all, reports included.  ``--yes`` applies without
asking (for non-interactive use); without it, a run whose stdin is not a terminal
refuses to write.  ``--backup`` keeps this run's originals under
``backups/<timestamp>/``: ``.lrc`` files in full, audio files as a tag dump.

Examples:
    ./music_lib.py                                  # interactive full pipeline
    ./music_lib.py --dry-run                        # show, write nothing at all
    ./music_lib.py --steps clean,bilingual --backup
    ./music_lib.py /path/to/Music --steps extract --yes
    ./music_lib.py --steps bilingual --only-duplicates
    ./music_lib.py --steps bilingual --max-lines 2
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TCOM, TEXT, TIPL, TMCL, TXXX, IPLS, USLT, ID3NoHeaderError
    from mutagen.oggvorbis import OggVorbis
except ImportError as _e:  # pragma: no cover - environment guard
    print(f"Error: could not import mutagen ({_e}). Try: pip install mutagen", file=sys.stderr)
    raise SystemExit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MUSIC_DIR = "/home/NeedtoPratice/Music"
# Everything this tool generates stays inside musicManage/: review reports here,
# backups under backups/<timestamp>/ (see README.md).
DEFAULT_REPORT_DIR = SCRIPT_DIR / "reports"
DEFAULT_BACKUP_DIR = SCRIPT_DIR / "backups"
# Editable rules (see README.md).  These are data, not code: adding a new source
# site notice or a new credit spelling means editing a text file, not this file.
RULES_DIR = SCRIPT_DIR / "rules"
MANUAL_DIR = SCRIPT_DIR / "manual"


def load_rule_pairs(path: Path) -> dict[str, str]:
    """Read ``key = value`` lines.  ``#`` only comments out a whole line."""
    out: dict[str, str] = {}
    if not path.is_file():
        print(f"Warning: rule file missing: {path}", file=sys.stderr)
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def load_rule_patterns(path: Path) -> list[re.Pattern[str]]:
    """Read one regular expression per line.  ``#`` only comments out a whole line."""
    out: list[re.Pattern[str]] = []
    if not path.is_file():
        print(f"Warning: rule file missing: {path}", file=sys.stderr)
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line))
        except re.error as e:
            print(f"Warning: bad pattern in {path}: {line!r} ({e})", file=sys.stderr)
    return out


def load_rule_names(path: Path) -> set[str]:
    """Read one name per line (used for the manual 'this is instrumental' list)."""
    if not path.is_file():
        print(f"Warning: rule file missing: {path}", file=sys.stderr)
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


# Files a human has declared instrumental: their .lrc body is discarded (after
# any recognisable credits are moved to tags) and rewritten as "[00:00.00]No lyrics".
MANUAL_INSTRUMENTAL = load_rule_names(MANUAL_DIR / "instrumental.txt")

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".mp4", ".m4b"}

# How many removed lines a --dry-run shows per file before collapsing.
DRY_RUN_PREVIEW_LINES = 8

# How many lines of a report the interactive prompt shows when you press Enter.
REPORT_PREVIEW_LINES = 60

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

STEP_ORDER = [
    "ogg", "extract", "clean", "nolyrics", "bilingual", "romaji",
    "tags", "embed", "rename",
]


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


def adopt_untimed_lines(body: list[str]) -> list[str]:
    """Give every untimed lyric line the timestamp of the line above it.

    This is what turns a bilingual file from

        [00:10.17]春がきた
        春天来了

    into the shape the library standard asks for -- every line of a group
    carrying that group's timestamp, so rmpc shows original, romaji and
    translation together:

        [00:10.17]春がきた
        [00:10.17]春天来了

    Only lines with **no** timestamp are touched, and only when a timestamp has
    already been seen; a line the parser can read is never rewritten.  Blank
    lines are copied through as they are and do **not** break the chain: a
    translation separated from its original by a blank line still belongs to it,
    and the blank simply ends up after the group in the output.
    """
    out: list[str] = []
    last_ts: str | None = None
    for line in body:
        s = line.strip()
        if not s:
            out.append(line)
            continue
        split = split_lrc_line(line)
        if split:
            timestamps, _content = split
            if timestamps and timestamp_to_ms(timestamps[0]) is not None:
                last_ts = timestamps[0]
            out.append(line)
            continue
        if LRC_HEADER_RE.match(s):
            # A header tag that turns up after the first lyric is not a lyric
            # continuation: give it a timestamp and it becomes a lyric line.
            # `clean` removes it; until then, leave it exactly as it is.
            out.append(line)
            continue
        out.append(f"[{last_ts}]{s}" if last_ts else line)
    return out


def regroup_body_lines(
    body: list[str],
    parse: Callable[[str], list[tuple[str, str]]],
    output_timestamp: Callable[[list[tuple[str, str]]], str],
    reorder: Callable[[list[tuple[str, str]]], list[tuple[str, str]]] | None = None,
    max_lines: int | None = None,
    adopt: bool = False,
) -> tuple[list[str], int, int]:
    """Pull same-timestamp lines together, keeping every other line untouched.

    Only lines the parser understands are regrouped.  Every other body line is
    copied through verbatim, because a real .lrc can hold lines this module
    cannot represent -- an untimed note, a timestamp with seconds > 59 such as
    ``[00:75.00]``, a header tag that appears after the first lyric.  Rebuilding
    the body from parsed groups alone would delete all of those silently.

    With ``adopt`` the untimed lyric lines are first given their predecessor's
    timestamp (see ``adopt_untimed_lines``) so translations and romaji join the
    group they belong to instead of sitting outside every group.

    ``output_timestamp`` receives a whole group and returns the bracketed
    timestamp to print for every line of that group.

    Returns (new_body, groups_reordered, lines_dropped_by_max_lines).
    """
    if adopt:
        body = adopt_untimed_lines(body)

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
    reports: list[Path] = field(default_factory=list)


def backup_dir_for(args: argparse.Namespace, path: Path) -> Path:
    """Where this file's backup goes: musicManage/backups/<run>/, tree mirrored."""
    try:
        rel = path.resolve().relative_to(args.music_root)
    except ValueError:
        rel = Path(path.name)
    return args.backup_dir / rel


def backup_text_file(path: Path, original: str, args: argparse.Namespace) -> Path | None:
    """Keep this run's copy of a text file, as it looked before the run started.

    Backups live under musicManage/backups/<timestamp>/ and mirror the library
    tree, instead of piling .bak files up next to the music.  The first backup of
    a run wins, so a file touched by several steps still has its pre-run content.
    """
    dest = backup_dir_for(args, path)
    if dest.exists():
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(original, encoding="utf-8")
    except OSError as e:
        print(f"    Warning: could not back up {path.name}: {e}", file=sys.stderr)
        return None
    return dest


def dump_tags(path: Path, args: argparse.Namespace) -> Path | None:
    """Back up a file's *tags* instead of copying the whole audio file.

    The library is ~5.6 GB, so a full copy per run is the wrong shape; a tag dump
    is a few kB and holds every value this tool can change.
    """
    dest = backup_dir_for(args, path).with_name(path.name + ".tags.txt")
    if dest.exists():
        return None
    try:
        audio = MutagenFile(path)
    except Exception as e:  # noqa: BLE001
        print(f"    Warning: could not read tags of {path.name}: {e}", file=sys.stderr)
        return None
    lines = [f"# tags of {path}", f"# container: {type(audio).__name__ if audio else 'unreadable'}"]
    if audio is not None and audio.tags is not None:
        for key in sorted(audio.tags.keys(), key=str):
            try:
                lines.append(f"{key} = {audio.tags[key]!r}")
            except Exception:  # noqa: BLE001
                lines.append(f"{key} = <unreadable>")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"    Warning: could not back up tags of {path.name}: {e}", file=sys.stderr)
        return None
    return dest


def write_report(
    report_dir: Path,
    filename: str,
    header: str,
    items: list[str],
    args: argparse.Namespace,
) -> Path | None:
    """Write a review report.

    Interactive runs write the report *before* asking whether to apply the step,
    so the decision is made with the full list in hand rather than from a
    summary.  ``--dry-run`` writes nothing at all, reports included.
    """
    if not items:
        return None
    if not args.write_reports:
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
        if args.apply:
            if args.backup:
                backed_up = dump_tags(path, args) is not None
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
                result.notes.append(f"tags backed up: {path.name}")
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
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
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

    if args.apply:
        # A write failure is this file's problem only; the step reports it and
        # carries on with the rest of the library.
        try:
            if args.backup and lrc_path.exists():
                original = lrc_path.read_text(encoding="utf-8", errors="replace")
                backup_text_file(lrc_path, original, args)
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
        report = write_report(args.report_dir, filename, header, items, args)
        if report:
            result.notes.append(f"report: {report}")
            result.reports.append(report)
    return finish(result)


# ---------------------------------------------------------------------------
# Step: move author/credit info out of .lrc and into the audio file's tags
# ---------------------------------------------------------------------------

# Normalized role (lowercase, all whitespace removed) -> canonical field name.
# Curated on purpose: lyric lines that merely contain a colon ("她说：...",
# "但我们最终归航：...") must never be mistaken for credits, so only these
# exact role spellings are accepted.  The list lives in rules/roles.txt.
CREDIT_ROLES: dict[str, str] = {}

# Normalized Chinese instrument role -> English instrument name.
# The list lives in rules/instruments.txt.
INSTRUMENT_ROLES: dict[str, str] = {}

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

# Every "[name:value]" line is a header tag, and none of them are lyrics: song
# title, artist, album, length, source-site ids (by/kuwo/ver/hash/sign/qq/
# total/kana) and the base64 blobs some sites embed ([awlrc:...]/[tlrc:...]).
# They are all removed, so a .lrc holds nothing but lyrics.
#
# This deliberately drops ti/ar/al/length, which rmpc can index lyrics by.  The
# trade is intentional: the same lyrics are embedded in the audio file and the
# file name is normalised to "<title> - <artist>", so matching keeps a path.
#
# The value part is ".*" rather than "[^\]]*" because real headers contain
# brackets -- "[ar:Black onyX [帝アキラ (cv.入野自由)...]]" used to fall through
# to the "is this a lyric?" branch and made an instrumental look sung.
LRC_HEADER_RE = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_]*):(.*)\]$")

# A bracketed prefix of any shape: catches credit lines behind a malformed
# timestamp such as "[00:-1.0]作词: X", which TIMESTAMP_RE cannot match.
BRACKETED_RE = re.compile(r"^\[([^\]]*)\](.*)$")


def normalize_role(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s).lower()


CREDIT_ROLES.update(
    {normalize_role(k): v for k, v in load_rule_pairs(RULES_DIR / "roles.txt").items()}
)
INSTRUMENT_ROLES.update(
    {normalize_role(k): v for k, v in load_rule_pairs(RULES_DIR / "instruments.txt").items()}
)

# Source-site legal notices some LRC files carry as a *lyric line*, e.g.
# "[00:00.45]QQ音乐享有本翻译作品的著作权".  Patterns are deliberately narrow --
# see rules/notices.txt for the measured false positives of a broad match.
NOTICE_PATTERNS = load_rule_patterns(RULES_DIR / "notices.txt")


def matching_notice(text: str) -> str | None:
    """Return the pattern that marks this text as a source-site notice."""
    for pat in NOTICE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


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
    """True for a leading "<artist> - <title>" / "<title> - <artist>" heading.

    Sources write the per-song heading either way round -- "羽生まゐご - 我愛メイデン"
    (artist first) and "我愛メイデン - 羽生まゐご" (title first) both occur, and the
    first spelling used to be kept as a lyric because only the tail was checked.
    The line counts as a heading when either side is a known artist name.
    """
    if " - " not in content or len(content) > 200:
        return False
    head_cmp, tail_cmp = (_norm_cmp(part) for part in content.split(" - ", 1))
    return any(
        len(a) >= 2 and (a in head_cmp or a in tail_cmp)
        for a in (_norm_cmp(n) for n in artist_names if n)
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
    """What would be taken out of one .lrc file."""

    credits: dict[str, list[str]] = field(default_factory=dict)
    remove_idx: set[int] = field(default_factory=set)
    removed_preview: list[str] = field(default_factory=list)

    def remove(self, idx: int, line: str, reason: str) -> None:
        self.remove_idx.add(idx)
        self.removed_preview.append(f"{line}   ({reason})")

    def note_credit(self, fields: list[str], value: str) -> None:
        for name in fields:
            self.credits.setdefault(name, []).append(value)

    def resolved(self) -> dict[str, str]:
        """Collapse accumulated values per field, de-duplicated and joined."""
        out: dict[str, str] = {}
        for field_name, values in self.credits.items():
            seen = list(dict.fromkeys(v for v in values if v))
            if seen:
                out[field_name] = " / ".join(seen)
        return out


def plan_credit_extraction(
    lines: list[str],
    artist_names: list[str],
    forced_instrumental: bool = False,
) -> CreditPlan:
    """Decide what to take out of one .lrc: everything that is not a lyric.

    Four kinds of line go:

    * **header tags** -- every ``[name:value]`` line: ti/ar/al/length, the
      source-site ids (by/kuwo/ver/hash/sign/qq/total/kana) and the base64 blobs
      some sites embed as ``[awlrc:...]`` / ``[tlrc:...]``;
    * **credits** -- ``作词：X``, ``Guitars: Y``, a leading ``<title> - <artist>``
      line; with a timestamp or without, and also behind a malformed timestamp
      such as ``[00:-1.0]`` that TIMESTAMP_RE cannot match;
    * **source-site notices** -- matched against ``rules/notices.txt``;
    * the ``//`` separator some sources emit.

    Everything else is kept, including blank lines and -- deliberately -- text
    lines with no timestamp: those are the translation/romaji continuations the
    ``bilingual`` step is about to give a timestamp to.  When in doubt the line
    stays, and nothing is dropped without appearing in the report.
    """
    plan = CreditPlan()
    # Title lines only appear in the leading block, and a source may carry
    # several of them (original plus a romaji transliteration), so eligibility
    # lasts until the first genuine lyric line rather than only index 0.
    in_leading_block = True

    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue  # blank line: kept, and it does not break the run of lyrics

        m = TIMESTAMP_RE.match(s)
        content = s[m.end():].strip() if m else s

        if m is None:
            if LRC_HEADER_RE.match(s):
                plan.remove(idx, line, "header tag")
                continue
            bracketed = BRACKETED_RE.match(s)
            if bracketed:
                rest = bracketed.group(2).strip()
                parsed = parse_credit_line(rest) if rest else None
                if parsed is not None:
                    plan.note_credit(*parsed)
                    plan.remove(idx, line, "credit behind a malformed timestamp")
                continue
        elif content in ("//", "/"):
            plan.remove(idx, line, "separator")
            continue

        if forced_instrumental:
            # A human declared this file instrumental (manual/instrumental.txt):
            # keep whatever credit is still recognisable, drop the rest -- but a
            # placeholder line is already the file's finished state, so removing
            # it would leave the .lrc empty and make nolyrics write it back.
            parsed = parse_credit_line(content) if content else None
            if parsed is not None:
                plan.note_credit(*parsed)
            if is_placeholder(content):
                continue
            plan.remove(idx, line, "declared instrumental")
            continue

        if not content:
            continue  # "[00:12.00]" with nothing after it: a display blank, kept

        notice = matching_notice(content)
        if notice:
            plan.remove(idx, line, f"source-site notice  /{notice}/")
            continue

        parsed = parse_credit_line(content)
        is_title = in_leading_block and looks_like_title_line(content, artist_names) and (
            m is None or m.group(1).startswith("00:00")
        )
        if parsed is None and not is_title:
            in_leading_block = False  # first real lyric line ends the heading block
            continue

        if is_title:
            plan.remove(idx, line, "title line")
        else:
            plan.note_credit(*parsed)  # type: ignore[misc]
            plan.remove(idx, line, "credit")
    return plan


def step_clean(args: argparse.Namespace, root: Path) -> StepResult:
    """Leave nothing in a .lrc but lyrics; move author info into audio tags."""
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")
    if MANUAL_INSTRUMENTAL:
        print(f"  Declared instrumental (manual/instrumental.txt): {len(MANUAL_INSTRUMENTAL)}")

    result = StepResult(
        "clean",
        counts={"cleaned": 0, "skipped_clean": 0, "skipped_no_audio": 0, "error": 0},
    )
    removed_log: list[str] = []
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
        had_bom = text.startswith("\ufeff")
        if had_bom:
            text = text[1:]
        lines = text.splitlines()

        audio_meta = get_metadata(audio)
        artist_tag = audio_meta[0]
        artist_names = [a.strip() for a in re.split(r"[;,/]", artist_tag)] if artist_tag else []
        for line in lines:
            m = re.match(r"\[ar:(.*)\]$", line.strip())
            if m:
                artist_names.extend(a.strip() for a in re.split(r"[;,/、]", m.group(1)))

        plan = plan_credit_extraction(
            lines, artist_names, forced_instrumental=lrc.name in MANUAL_INSTRUMENTAL
        )
        resolved = plan.resolved()
        if not plan.remove_idx and not had_bom:
            result.counts["skipped_clean"] += 1
            continue

        if plan.remove_idx:
            removed_log.append(f"--- {lrc.name}")
            removed_log.extend(f"    - {entry}" for entry in plan.removed_preview)
            result.counts["lines_removed"] = (
                result.counts.get("lines_removed", 0) + len(plan.remove_idx)
            )

        if not args.apply:
            if plan.remove_idx:
                print(f"    WOULD CLEAN: {lrc.name}")
                for entry in plan.removed_preview[:DRY_RUN_PREVIEW_LINES]:
                    print(f"        - {entry}")
                extra = len(plan.removed_preview) - DRY_RUN_PREVIEW_LINES
                if extra > 0:
                    print(f"        ... (+{extra} more lines)")
            result.counts["cleaned"] += 1
            continue

        # Write the tags FIRST; only strip the .lrc once that succeeded.
        if resolved:
            try:
                change_lines = write_credits_to_audio(audio, resolved)
            except Exception as e:  # noqa: BLE001 - never lose lyrics on failure
                result.counts["error"] += 1
                result.errors.append(f"{audio.name}: tag write failed ({e}); .lrc untouched")
                continue
            move_log.append(f"{lrc.name}  ->  {audio.name}")
            move_log.extend(change_lines)

        if plan.remove_idx or had_bom:
            kept = [ln for idx, ln in enumerate(lines) if idx not in plan.remove_idx]
            new_text = "\n".join(kept)
            if text.endswith("\n"):
                new_text += "\n"
            # Tags are already written, so a failure here only means the removed
            # lines stay in the .lrc (duplicated, nothing lost).
            try:
                if args.backup:
                    backup_text_file(lrc, text, args)
                lrc.write_text(new_text, encoding="utf-8")
            except OSError as e:
                result.counts["error"] += 1
                result.errors.append(f"{lrc}: cannot write: {e}")
                continue

        result.counts["cleaned"] += 1

    report_summary(
        result.counts,
        [
            ("cleaned", "Cleaned:"),
            ("lines_removed", "Lines removed:"),
            ("skipped_clean", "Skipped (nothing to remove):"),
            ("skipped_no_audio", "Skipped (no matching audio):"),
            ("error", "Errors:"),
        ],
    )
    for filename, header, items in (
        (
            "clean_removed_report.txt",
            "Every line removed from a .lrc, with the reason.  '--- <file>' starts "
            "each file's list.",
            removed_log,
        ),
        (
            "clean_tags_report.txt",
            "Author info moved from .lrc into audio tags. '[+]' added, "
            "'[~]' overwritten, '[=]' unchanged; 'old -> new' shows what was replaced.",
            move_log,
        ),
    ):
        report = write_report(args.report_dir, filename, header, items, args)
        if report:
            result.notes.append(f"report: {report}")
            result.reports.append(report)
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
    """True for a header tag line such as [ti:...], [ar:...], [by:...].

    Uses the same permissive pattern as the cleaner: a header value may itself
    contain brackets ("[ar:Black onyX [帝アキラ (cv.入野自由)・...]]"), and such a
    line used to fall through to the "is this a lyric?" branch and make an
    instrumental look sung.
    """
    return bool(LRC_HEADER_RE.match(line.strip()))


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


NO_LYRICS_LINE = "[00:00.00]No lyrics"


def build_no_lyrics_lrc() -> str:
    """The only content a lyric-less .lrc may carry.

    One line and nothing else -- no [ti:]/[ar:]/[al:]/[length:] header, because
    a .lrc holds lyrics and nothing but lyrics.  The title/artist/album live in
    the audio file's tags and in its name.
    """
    return NO_LYRICS_LINE + "\n"


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
    declared = lrc_path.name in MANUAL_INSTRUMENTAL

    if lrc_path.exists():
        try:
            existing = lrc_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"error: {e}"

        # A file declared instrumental is "no lyrics" by definition; otherwise
        # ask whether anything left in it is a real lyric.
        if not declared and has_real_lyrics_in_text(existing):
            return "skipped_has_lyrics"

        new_content = build_no_lyrics_lrc()
        if existing == new_content:
            return "skipped_no_change"
        if not args.apply:
            return "written"
        try:
            if args.backup:
                backup_text_file(lrc_path, existing, args)
            lrc_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"error: {e}"
        return "written"

    # No .lrc file.  Check embedded lyrics so real lyrics are not marked none.
    if not declared:
        embedded = extract_embedded_text(path)
        if embedded and has_real_lyrics_in_text(embedded):
            return "skipped_has_lyrics"

    new_content = build_no_lyrics_lrc()
    if not args.apply:
        return "written"
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

    written_log: list[str] = []

    for i, path in enumerate(files, 1):
        status = process_no_lyrics(path, args)
        if status.startswith("error"):
            result.counts["error"] += 1
            result.errors.append(f"{path}: {status[7:]}")
        else:
            result.counts[status] = result.counts.get(status, 0) + 1
            if status == "written":
                written_log.append(path.with_suffix(".lrc").name)
                if not args.apply:
                    print(f"    WOULD WRITE: {path.with_suffix('.lrc').name}")
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
    report = write_report(
        args.report_dir,
        "nolyrics_report.txt",
        "Songs with no real lyrics.  Each one's .lrc is (or would be) replaced by "
        "the single line '[00:00.00]No lyrics'.",
        written_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
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
        adopt=True,
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
    changed_log: list[str] = []

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

        before_set = {line.strip() for line in original.splitlines()}
        gained = [
            line
            for line in new_text.splitlines()
            if line.strip() and line.strip() not in before_set
        ]
        if gained:
            changed_log.append(f"--- {path.name}   ({len(gained)} line(s) given a timestamp)")
            changed_log.extend(f"    {line}" for line in gained)
        else:
            changed_log.append(f"--- {path.name}   (lines regrouped)")

        if not args.apply:
            print(f"    WOULD UPDATE: {path.name}")
            for line in gained[:DRY_RUN_PREVIEW_LINES]:
                print(f"        + {line}")
            if len(gained) > DRY_RUN_PREVIEW_LINES:
                print(f"        ... (+{len(gained) - DRY_RUN_PREVIEW_LINES} more lines)")
            result.counts["written"] += 1
            continue

        # One unwritable file must not abort the remaining files or steps.
        try:
            if args.backup:
                backup_text_file(path, original, args)
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
    report = write_report(
        args.report_dir,
        "bilingual_report.txt",
        "Lines that had no timestamp and were given their neighbour's, which is what "
        "puts an original, its romaji and its translation at the same moment.",
        changed_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
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
        adopt=True,
    )

    if not reordered:
        return None
    return render(header, new_body, text.endswith("\n"))


def step_romaji(args: argparse.Namespace, root: Path) -> StepResult:
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")

    result = StepResult("romaji", counts={"written": 0, "skipped": 0, "error": 0})
    changed_log: list[str] = []

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

        before_set = {line.strip() for line in original.splitlines()}
        moved = [
            line
            for line in new_text.splitlines()
            if line.strip() and line.strip() not in before_set
        ]
        changed_log.append(f"--- {path.name}")
        changed_log.extend(f"    {line}" for line in moved)

        if not args.apply:
            print(f"    WOULD UPDATE: {path.name}")
            for line in moved[:DRY_RUN_PREVIEW_LINES]:
                print(f"        {line}")
            if len(moved) > DRY_RUN_PREVIEW_LINES:
                print(f"        ... (+{len(moved) - DRY_RUN_PREVIEW_LINES} more lines)")
            result.counts["written"] += 1
            continue

        # One unwritable file must not abort the remaining files or steps.
        try:
            if args.backup:
                backup_text_file(path, original, args)
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
    report = write_report(
        args.report_dir,
        "romaji_report.txt",
        "Groups whose romaji line was moved in front of the translation.",
        changed_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
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
# Step: normalise the tags the file name is built from
# ---------------------------------------------------------------------------


# A trailing "(...)" that holds only Han characters, on a title whose head is
# kana: "銀の龍の背に乗って (骑在银龙的背上)" -> "銀の龍の背に乗って".  Deliberately
# narrow -- "ray (超かぐや姫！ Version)", "(feat. 茶太)" and "(5人Ver.)" all keep
# their brackets, because the inside carries kana or Latin.
TRAILING_GLOSS_RE = re.compile(r"\s*[（(]\s*([^（()）]*?)\s*[)）]\s*$")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")

# A multi-artist value gets one spelling: "A、B" and "A, B" both become "A;B".
# Measured before switching it on: the library has 2 values containing ", " and
# both really are two artists, and 0 containing " & " -- so there is no
# "Earth, Wind & Fire" to break.  Every change lands in the report either way.
ARTIST_SEP_RE = re.compile(r"\s*[、;]\s*|\s*,\s*")


def strip_chinese_gloss(title: str) -> tuple[str, str | None]:
    """Return (title without its Chinese translation, the removed gloss or None)."""
    m = TRAILING_GLOSS_RE.search(title)
    if not m:
        return title, None
    inner, head = m.group(1), title[: m.start()]
    if (
        KANA_RE.search(head)
        and HAN_RE.search(inner)
        and not KANA_RE.search(inner)
        and not LATIN_RE.search(inner)
    ):
        return head.strip(), inner
    return title, None


def normalize_artist(value: str) -> str:
    return ARTIST_SEP_RE.sub(";", value.strip())


def filename_title_artist(stem: str, tag_title: str = "") -> tuple[str | None, str | None]:
    """(title, artist) as the file name spells them, per '<title> - <artist>'.

    A title can itself contain " - " ("Four Seasons - Spring - At Vance"), so the
    split is at the **last** separator, and when the tag's title spells the whole
    prefix that spelling is trusted outright.
    """
    if " - " not in stem:
        return None, None
    if tag_title and stem.startswith(f"{tag_title} - "):
        return tag_title, stem[len(tag_title) + 3:].strip() or None
    head, tail = stem.rsplit(" - ", 1)
    return head.strip() or None, tail.strip() or None


@dataclass
class TagPlan:
    """The tag fixes one audio file needs, and why."""

    changes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def desired_name_fields(path: Path) -> tuple[str, str, list[str]]:
    """The title and artist the file name should be built from, plus why.

    One rule, shared by ``tags`` and ``rename``, so the two can never disagree:
    **the file name is the authority on the artist** -- a tag that was tidied up
    (a compilation's "metals" standing in for the real performers) gets the
    name's spelling back -- and **the tag is the authority on the title**, with
    two cleanups: a Chinese translation bolted on the end, and a title that
    already repeats " - <artist>" (some tags carry the whole heading).
    """
    notes: list[str] = []
    tag_title = tag_artist = ""
    audio = MutagenFile(path, easy=True)
    if audio is not None:
        raw_title = audio.get("title")
        raw_artist = audio.get("artist")
        if isinstance(raw_title, list) and raw_title:
            tag_title = str(raw_title[0]).strip()
        if isinstance(raw_artist, list) and raw_artist:
            tag_artist = str(raw_artist[0]).strip()

    fn_title, fn_artist = filename_title_artist(path.stem, tag_title)

    title = tag_title
    if not title and fn_title:
        title = fn_title
        notes.append(f"title from the file name: {fn_title!r}")

    artist = normalize_artist(fn_artist) if fn_artist else tag_artist

    title, gloss = strip_chinese_gloss(title)
    if gloss:
        notes.append(f"dropped the Chinese gloss {gloss!r}")

    if artist and title.endswith(f" - {artist}"):
        title = title[: -len(f" - {artist}")].strip()
        notes.append(f"dropped the trailing ' - {artist}' from the title")

    return title, artist, notes


def plan_tag_normalization(path: Path) -> TagPlan:
    """Work out the title/artist/album-artist/track fixes for one audio file."""
    plan = TagPlan()
    audio = MutagenFile(path, easy=True)
    if audio is None:
        return plan

    def get(key: str) -> str:
        value = audio.get(key)
        if isinstance(value, list):
            return str(value[0]).strip() if value else ""
        return str(value).strip() if value else ""

    current_title = get("title")
    current_artist = get("artist")
    title, artist, notes = desired_name_fields(path)

    if title and title != current_title:
        plan.changes["title"] = title
        plan.notes.append(f"title: {current_title or '(empty)'} -> {title}")
    if artist and artist != current_artist:
        plan.changes["artist"] = artist
        plan.notes.append(f"artist: {current_artist or '(empty)'} -> {artist}")
        # The value being replaced is not thrown away: it becomes the album
        # artist, unless one is already set.
        if current_artist and not get("albumartist"):
            plan.changes["albumartist"] = current_artist
            plan.notes.append(f"album artist: (empty) -> {current_artist}")

    track = get("tracknumber")
    if re.fullmatch(r"0\d+", track):
        plan.changes["tracknumber"] = str(int(track))
        plan.notes.append(f"tracknumber: {track} -> {plan.changes['tracknumber']}")

    plan.notes.extend(notes)
    return plan


def apply_tag_changes(path: Path, changes: dict[str, str]) -> None:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise OSError("mutagen could not open file")
    # EasyID3 / EasyMP4 / Vorbis all take "key = [value]", and saving keeps the
    # frames this tool does not manage: cover art, the credits the clean step
    # wrote, the embedded lyrics.
    for key, value in changes.items():
        audio[key] = [value]
    audio.save()


def step_tags(args: argparse.Namespace, root: Path) -> StepResult:
    """Make the tags agree with the file name: title, artist, album artist, track."""
    files = audio_files(root)
    print(f"  Found {len(files)} audio files under {root}")

    result = StepResult("tags", counts={"updated": 0, "skipped_ok": 0, "error": 0})
    change_log: list[str] = []

    for i, path in enumerate(files, 1):
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")
        try:
            plan = plan_tag_normalization(path)
        except Exception as e:  # noqa: BLE001 - per-file isolation
            result.counts["error"] += 1
            result.errors.append(f"{path.name}: {e}")
            continue
        if not plan.changes:
            result.counts["skipped_ok"] += 1
            continue

        change_log.append(f"--- {path.name}")
        change_log.extend(f"    {note}" for note in plan.notes)

        if not args.apply:
            print(f"    WOULD TAG: {path.name}")
            for note in plan.notes[:DRY_RUN_PREVIEW_LINES]:
                print(f"        - {note}")
            result.counts["updated"] += 1
            continue

        try:
            if args.backup:
                dump_tags(path, args)
            apply_tag_changes(path, plan.changes)
        except Exception as e:  # noqa: BLE001
            result.counts["error"] += 1
            result.errors.append(f"{path.name}: {e}")
            continue
        result.counts["updated"] += 1

    report_summary(
        result.counts,
        [
            ("updated", "Tag sets updated:"),
            ("skipped_ok", "Skipped (already agree):"),
            ("error", "Errors:"),
        ],
    )
    report = write_report(
        args.report_dir,
        "tags_report.txt",
        "Tag fixes, so that the file name '<title> - <artist>' is what the tags say. "
        "An artist taken from the file name replaces a tidied-up tag; the value it "
        "replaced is kept as the album artist.",
        change_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
    return finish(result)


# ---------------------------------------------------------------------------
# Step: carry the .lrc back into the audio file
# ---------------------------------------------------------------------------


def write_embedded_lyrics(path: Path, text: str) -> None:
    """Store timed lyrics in the audio file's own lyrics field."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        # Exactly one lyrics frame: leaving an older one behind makes readers
        # pick whichever they happen to hit first.
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        tags.save(path)
        return
    audio = MutagenFile(path)
    if audio is None:
        raise OSError("mutagen could not open file")
    key = "\xa9lyr" if ext in {".m4a", ".mp4", ".m4b"} else "LYRICS"
    audio[key] = [text]
    audio.save()


def step_embed(args: argparse.Namespace, root: Path) -> StepResult:
    """Put the cleaned .lrc into the audio file, so both carry the same lyrics."""
    files = lrc_files(root)
    print(f"  Found {len(files)} .lrc files under {root}")

    result = StepResult(
        "embed",
        counts={"embedded": 0, "skipped_identical": 0, "skipped_no_audio": 0, "error": 0},
    )
    change_log: list[str] = []

    for i, lrc in enumerate(files, 1):
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

        audio = find_audio_for_lrc(lrc)
        if audio is None:
            result.counts["skipped_no_audio"] += 1
            continue
        try:
            text = lrc.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{lrc.name}: {e}")
            continue

        current = extract_embedded_text(audio)
        if current is not None and current.strip() == text.strip():
            result.counts["skipped_identical"] += 1
            continue

        change_log.append(f"{audio.name}  <-  {lrc.name}   ({len(text)} bytes)")

        if not args.apply:
            print(f"    WOULD EMBED: {audio.name}")
            result.counts["embedded"] += 1
            continue

        try:
            if args.backup:
                dump_tags(audio, args)
            write_embedded_lyrics(audio, text)
        except Exception as e:  # noqa: BLE001
            result.counts["error"] += 1
            result.errors.append(f"{audio.name}: {e}")
            continue
        result.counts["embedded"] += 1

    report_summary(
        result.counts,
        [
            ("embedded", "Lyrics embedded:"),
            ("skipped_identical", "Skipped (already identical):"),
            ("skipped_no_audio", "Skipped (no matching audio):"),
            ("error", "Errors:"),
        ],
    )
    report = write_report(
        args.report_dir,
        "embed_report.txt",
        "The .lrc written into the audio file's own lyrics field: .mp3 -> USLT, "
        ".flac/.ogg -> LYRICS, .m4a -> (c)lyr.",
        change_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
    return finish(result)


# ---------------------------------------------------------------------------
# Step: file name = "<title> - <artist>"
# ---------------------------------------------------------------------------


def name_problem(stem: str) -> str | None:
    """Why this cannot be a file name, or None when it is fine.

    A "/" in a title would quietly turn the rename into a write into another
    directory, so the file is **left alone and reported** rather than renamed to
    a mangled spelling -- "EXEC_COSMOFLIPS/." should not become
    "EXEC_COSMOFLIPS\uff0f.".  Trailing spaces and dots are trimmed, which loses
    nothing.  In this library 4 files hit the "/" case.
    """
    for bad, what in (("/", "a '/'"), ("\x00", "a NUL")):
        if bad in stem:
            return f"name would contain {what}, left alone: {stem!r}"
    if not stem:
        return "name would be empty"
    return None


def step_rename(args: argparse.Namespace, root: Path) -> StepResult:
    """Rename every audio file -- and its .lrc -- to "<title> - <artist>"."""
    files = audio_files(root)
    print(f"  Found {len(files)} audio files under {root}")

    result = StepResult(
        "rename",
        counts={
            "renamed": 0,
            "skipped_ok": 0,
            "skipped_no_title": 0,
            "skipped_conflict": 0,
            "skipped_bad_name": 0,
            "error": 0,
        },
    )
    change_log: list[str] = []
    claimed: dict[str, str] = {}

    for i, path in enumerate(files, 1):
        if i % 100 == 0 or i == len(files):
            print(f"    ...{i}/{len(files)} processed")

        try:
            # Same rule as the tags step, so running rename on its own gives the
            # same names as running it after tags -- an artist tag that was
            # tidied up must not undo the name the file already carries.
            title, artist, _notes = desired_name_fields(path)
        except Exception as e:  # noqa: BLE001
            result.counts["error"] += 1
            result.errors.append(f"{path.name}: {e}")
            continue

        if not title:
            result.counts["skipped_no_title"] += 1
            result.notes.append(f"no title tag; name left alone: {path.name}")
            continue

        target_stem = (f"{title} - {artist}" if artist else title).strip().rstrip(".")
        problem = name_problem(target_stem)
        if problem:
            result.counts["skipped_bad_name"] += 1
            result.notes.append(f"{problem}   (wanted by {path.name})")
            continue
        if target_stem == path.stem:
            result.counts["skipped_ok"] += 1
            continue

        target = path.with_name(target_stem + path.suffix)
        if target.exists():
            result.counts["skipped_conflict"] += 1
            result.notes.append(f"name taken: {target.name}   (wanted by {path.name})")
            continue
        if target_stem in claimed:
            result.counts["skipped_conflict"] += 1
            result.notes.append(
                f"two songs want {target.name}: {claimed[target_stem]} and {path.name}"
            )
            continue
        claimed[target_stem] = path.name

        change_log.append(f"{path.name}  ->  {target.name}")

        if not args.apply:
            print(f"    WOULD RENAME: {path.name}  ->  {target.name}")
            result.counts["renamed"] += 1
            continue

        lrc = path.with_suffix(".lrc")
        new_lrc = target.with_suffix(".lrc")
        try:
            path.rename(target)
            if lrc.exists():
                if new_lrc.exists():
                    result.notes.append(f"left the .lrc alone: {new_lrc.name} exists")
                else:
                    lrc.rename(new_lrc)
        except OSError as e:
            result.counts["error"] += 1
            result.errors.append(f"{path.name}: {e}")
            continue
        result.counts["renamed"] += 1

    report_summary(
        result.counts,
        [
            ("renamed", "Renamed:"),
            ("skipped_ok", "Skipped (name already right):"),
            ("skipped_no_title", "Skipped (no title tag):"),
            ("skipped_conflict", "Skipped (name taken):"),
            ("skipped_bad_name", "Skipped (impossible name):"),
            ("error", "Errors:"),
        ],
    )
    report = write_report(
        args.report_dir,
        "rename_report.txt",
        "Every audio file renamed to '<title> - <artist>', with its .lrc alongside.",
        change_log,
        args,
    )
    if report:
        result.notes.append(f"report: {report}")
        result.reports.append(report)
    return finish(result)


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change and write nothing at all -- no library "
        "change, no report file, no backup",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask before each step (for non-interactive runs).  Reports "
        "and backups are still written",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help=f"Back up before changing anything: .lrc files in full, audio files "
        f"as a tag dump, under {DEFAULT_BACKUP_DIR}/<timestamp>/",
    )
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
    "clean": step_clean,
    "nolyrics": step_no_lyrics,
    "bilingual": step_bilingual,
    "romaji": step_romaji,
    "tags": step_tags,
    "embed": step_embed,
    "rename": step_rename,
}


STEP_PROMPT_HELP = "[Enter] 看报告   [y] 执行   [s] 跳过本步   [q] 退出"
STEP_PROMPT_ANSWERS = {"y": "apply", "yes": "apply", "s": "skip", "n": "skip", "q": "quit"}


def show_report(path: Path) -> None:
    """Print the head of a report on Enter, leaving the file on disk for the rest."""
    lines = [f"  --- {path} ---"]
    try:
        lines.extend(Path(path).read_text(encoding="utf-8").splitlines())
    except OSError as e:
        print(f"  (cannot read {path}: {e})")
        return
    for line in lines[:REPORT_PREVIEW_LINES]:
        print(line if line.startswith("  ") else f"  {line}")
    if len(lines) > REPORT_PREVIEW_LINES:
        print(f"  ... ({len(lines)} lines in total; the full list is in {path})")


def ask_step(name: str, result: StepResult) -> str:
    """Ask whether to apply one step.  Returns 'apply' | 'skip' | 'quit'.

    The step has already run in plan mode, so its report is on disk: Enter shows
    the head of it and asks again, which is what makes the decision informed
    rather than a rubber stamp on a summary.
    """
    while True:
        try:
            answer = input(f"  [{name}] {STEP_PROMPT_HELP} > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if answer in STEP_PROMPT_ANSWERS:
            return STEP_PROMPT_ANSWERS[answer]
        if answer == "":
            if result.reports:
                for path in result.reports:
                    show_report(path)
            else:
                print("  (this step produced no report)")
            continue
        print(f"  Please answer Enter / y / s / q.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    steps = resolve_steps(args.steps)

    root = Path(args.music_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2
    args.music_root = root

    interactive = sys.stdin.isatty()
    if not args.dry_run and not args.yes and not interactive:
        print(
            "Refusing to run: stdin is not a terminal, so no step could be "
            "confirmed.  Run it in a terminal, or pass --yes to apply without "
            "asking, or --dry-run to only look.",
            file=sys.stderr,
        )
        return 2

    # One directory per run, so a re-run never lands on top of the previous
    # run's reports or backups.  Two runs can start within the same second (a
    # test suite, a quick retry), so keep bumping the suffix until it is free.
    report_parent = Path(args.report_dir).expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_id, suffix = stamp, 2
    while (report_parent / run_id).exists():
        run_id, suffix = f"{stamp}-{suffix}", suffix + 1
    args.report_dir = report_parent / run_id
    args.backup_dir = DEFAULT_BACKUP_DIR / run_id
    args.write_reports = not args.dry_run
    args.apply = False  # flipped per step, once the step is confirmed

    print("=" * 60)
    print(f" Music library: {root}")
    print(f" Steps:         {', '.join(steps)}")
    print(f" Mode:          {'dry-run (nothing is written)' if args.dry_run else ('apply, no questions' if args.yes else 'interactive')}")
    print(f" Backup:        {'yes -> ' + str(args.backup_dir) if args.backup else 'no'}")
    print(f" Reports:       {args.report_dir if args.write_reports else '(dry-run: none written)'}")
    print("=" * 60)

    failures = 0
    aborted_in: str | None = None
    stopped_by_user = False

    for name in steps:
        print(f"\n=====> [{name}]")

        # Interactive runs plan first: the step writes its report and changes
        # nothing, so the report is on disk by the time you are asked.  A
        # --dry-run run stops there; a --yes run goes straight to applying,
        # because there is nobody to ask and the report is written either way.
        if not args.yes:
            args.apply = False
            try:
                planned = RUNNERS[name](args, root)
            except Exception as e:  # noqa: BLE001 - a failed step must stop the run
                print(f"  STEP FAILED: {e}", file=sys.stderr)
                failures += 1
                aborted_in = name
                break
            if planned.errors:
                failures += 1

            if args.dry_run:
                continue

            decision = ask_step(name, planned)
            if decision == "quit":
                stopped_by_user = True
                break
            if decision == "skip":
                print(f"  [{name}] skipped.")
                continue

        # Apply, re-deriving the plan file by file as it goes.
        args.apply = True
        try:
            result = RUNNERS[name](args, root)
        except Exception as e:  # noqa: BLE001
            print(f"  STEP FAILED: {e}", file=sys.stderr)
            failures += 1
            args.apply = False
            aborted_in = name
            break
        args.apply = False
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
    if stopped_by_user:
        print(" Stopped at your request; later steps were not run.")
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
