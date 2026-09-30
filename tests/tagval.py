"""Print one easy-tag value of an audio file, for the regression suite.

    python3 tests/tagval.py <file> <key>

Prints an empty line when the tag is missing, so `[ "$(tagval ...)" = "x" ]`
works without special-casing.
"""
import sys

from mutagen import File as MutagenFile

value = None
audio = MutagenFile(sys.argv[1], easy=True)
if audio is not None:
    value = audio.get(sys.argv[2])
if isinstance(value, list):
    value = value[0] if value else None
print(value if value is not None else "")
