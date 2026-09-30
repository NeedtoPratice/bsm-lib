#!/usr/bin/env bash
# Regression suite for music_lib.py. Builds a synthetic library under tests/regress/
# and never touches ~/Music.
#
# Every case runs with --yes because a non-interactive run refuses to write
# otherwise (that refusal is itself case T13).
cd "$(dirname "$0")/.." || exit 1   # musicManage/
S=tests
R=$S/regress
ML=./music_lib.py
FLAGS="--yes --no-refresh"
pass=0; fail=0

ok()   { pass=$((pass+1)); echo "  PASS  $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL  $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$3] got [$2])"; fi; }
run()  { $ML "$@" $FLAGS >/dev/null 2>&1; }

rm -rf "$R"; mkdir -p "$R/reports"
sine() { ffmpeg -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=3" -map_metadata -1 -c:a "$1" "${@:2}"; }
tag()  { python3 -c "
from mutagen.id3 import ID3, TIT2, TPE1
t=ID3(); t.add(TIT2(encoding=3, text=['$2'])); t.add(TPE1(encoding=3, text=['$3'])); t.save('$1')"; }

echo "== T1 bilingual gives an untimed line its neighbour's timestamp =="
mkdir -p "$R/t1"; sine libmp3lame "$R/t1/a.mp3"
cat > "$R/t1/a.lrc" <<'EOF'
[ti:T1]
[00:01.00]Orig
[00:04.00]Other
[00:01.00]译文
Follow us: example.com
[00:75.00]Seconds out of range
EOF
run "$R/t1" --steps bilingual --report-dir "$R/reports"
check "exit 0" "$?" "0"
check "untimed line adopted"  "$(grep -c '^\[00:01.00\]Follow us: example.com$' "$R/t1/a.lrc")" "1"
check "no bare untimed left"  "$(grep -c '^Follow us: example.com$' "$R/t1/a.lrc")" "0"
check "out-of-range kept"     "$(grep -c '^\[00:75.00\]Seconds out of range$' "$R/t1/a.lrc")" "1"
check "translation grouped"   "$(sed -n '3p' "$R/t1/a.lrc")" "[00:01.00]译文"
check "adopted line grouped"  "$(sed -n '4p' "$R/t1/a.lrc")" "[00:01.00]Follow us: example.com"

echo "== T2 romaji reorders a 3-line group, keeps what it cannot parse =="
mkdir -p "$R/t2"; sine libmp3lame "$R/t2/a.mp3"
cat > "$R/t2/a.lrc" <<'EOF'
[ti:T2]
[00:01.00]Orig
[00:01.00]译文
[00:01.00]Romaji
[00:75.00]Seconds out of range
[by:someone]
EOF
run "$R/t2" --steps romaji --report-dir "$R/reports"
check "exit 0" "$?" "0"
check "orig first"   "$(sed -n '2p' "$R/t2/a.lrc")" "[00:01.00]Orig"
check "romaji second" "$(sed -n '3p' "$R/t2/a.lrc")" "[00:01.00]Romaji"
check "chinese last"  "$(sed -n '4p' "$R/t2/a.lrc")" "[00:01.00]译文"
check "out-of-range kept" "$(grep -c '^\[00:75.00\]Seconds out of range$' "$R/t2/a.lrc")" "1"
check "late header kept"  "$(grep -c '^\[by:someone\]$' "$R/t2/a.lrc")" "1"

echo "== T3 clean reaches nested .lrc and moves the credit into the tag =="
mkdir -p "$R/t3/deep/deeper"
sine libmp3lame "$R/t3/deep/deeper/song.mp3"
python3 -c "
from mutagen.id3 import ID3, USLT
t=ID3(); t.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]作词：嵌套作者\n[00:02.00]Real lyric')); t.save('$R/t3/deep/deeper/song.mp3')"
$ML "$R/t3" --steps extract,clean $FLAGS --report-dir "$R/reports" > "$R/t3.log" 2>&1
check "nested lrc found" "$(grep -c 'Found 1 .lrc files' "$R/t3.log")" "1"
check "credit stripped"  "$(grep -c '作词' "$R/t3/deep/deeper/song.lrc")" "0"
check "credit in tag"    "$(python3 -c "
from mutagen.id3 import ID3; print(len(ID3('$R/t3/deep/deeper/song.mp3').getall('TEXT')))")" "1"

echo "== T4 one unwritable file does not abort the run =="
mkdir -p "$R/t4"; sine libmp3lame "$R/t4/b.mp3"
cat > "$R/t4/b.lrc" <<'EOF'
[00:01.00]One
[00:03.00]Three
[00:01.00]Two
EOF
chmod 444 "$R/t4/b.lrc"
$ML "$R/t4" --steps bilingual,romaji $FLAGS --report-dir "$R/reports" > "$R/t4.log" 2> "$R/t4.err"
rc=$?
chmod 644 "$R/t4/b.lrc"
check "exit 1"               "$rc" "1"
check "error reported"       "$(grep -c 'cannot write' "$R/t4.log")" "1"
check "romaji still ran"     "$(grep -c '=====> \[romaji\]' "$R/t4.log")" "1"
check "abort wording absent" "$(grep -c 'Run aborted' "$R/t4.log")" "0"

echo "== T5 --max-lines drops are reported =="
mkdir -p "$R/t5"; sine libmp3lame "$R/t5/c.mp3"
printf '[00:01.00]L1\n[00:01.00]L2\n[00:01.00]L3\n[00:01.00]L4\n' > "$R/t5/c.lrc"
$ML "$R/t5" --steps bilingual --max-lines 2 $FLAGS --report-dir "$R/reports" > "$R/t5.log" 2>&1
check "truncation counted" "$(grep -c 'Lines dropped by --max-lines:   2' "$R/t5.log")" "1"
check "note printed"       "$(grep -c 'note: --max-lines dropped' "$R/t5.log")" "1"

echo "== T6 nolyrics writes exactly one line =="
mkdir -p "$R/t6"; sine flac "$R/t6/i.flac"
printf '[ti:Keep Me]\n[ar:Keep Artist]\n[al:Album]\n[length:03:00.00]\n[00:00.00]纯音乐，请欣赏\n' > "$R/t6/i.lrc"
run "$R/t6" --steps nolyrics --report-dir "$R/reports"
check "single line"  "$(cat "$R/t6/i.lrc")" "[00:00.00]No lyrics"
check "one line only" "$(wc -l < "$R/t6/i.lrc")" "1"
$ML "$R/t6" --steps nolyrics $FLAGS --report-dir "$R/reports" > "$R/t6.log" 2>&1
check "idempotent" "$(grep -cE 'Skipped \(already correct\): +1$' "$R/t6.log")" "1"

echo "== T7 dry-run writes nothing at all, reports included =="
mkdir -p "$R/t7"; sine libmp3lame "$R/t7/d.mp3"
tag "$R/t7/d.mp3" D "D Artist"
before=$(python3 "$S/hash.py" "$R/t7")
mkdir -p "$R/reports7"
$ML "$R/t7" --dry-run --report-dir "$R/reports7" > "$R/t7.log" 2>&1
after=$(python3 "$S/hash.py" "$R/t7")
check "no writes"        "$([ "$before" = "$after" ] && echo same)" "same"
check "no lrc created"   "$([ -f "$R/t7/d.lrc" ] && echo yes || echo no)" "no"
check "report announced" "$(grep -c 'Would write report lrc_missing_report.txt' "$R/t7.log")" "1"
check "no report file"   "$(ls "$R/reports7" | wc -l)" "0"

echo "== T8 full pipeline is idempotent =="
mkdir -p "$R/t8"; sine libvorbis "$R/t8/e.ogg"; sine libmp3lame "$R/t8/f.mp3"
python3 -c "
from mutagen.id3 import ID3, USLT
t=ID3(); t.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]作词：A\n[00:03.00]Real line')); t.save('$R/t8/f.mp3')"
$ML "$R/t8" $FLAGS --report-dir "$R/reports" > "$R/t8.1.log" 2>&1
h1=$(python3 "$S/hash.py" "$R/t8")
$ML "$R/t8" $FLAGS --report-dir "$R/reports" > "$R/t8.2.log" 2>&1
h2=$(python3 "$S/hash.py" "$R/t8")
check "first run wrote"  "$(grep -c 'Wrote title:                    1' "$R/t8.1.log")" "1"
check "second run no-op" "$(grep -c 'Updated:                        0' "$R/t8.2.log")" "2"
check "bytes stable"     "$([ "$h1" = "$h2" ] && echo same)" "same"

echo "== T9 refresh policy =="
$ML "$R/t8" --steps ogg --dry-run --report-dir "$R/reports" 2>&1 | grep -q "skipped in dry-run" && ok "dry-run skips refresh" || bad "dry-run skips refresh"
$ML "$R/t8" --steps ogg --no-refresh $FLAGS --report-dir "$R/reports" 2>&1 | grep -q "skipped: --no-refresh" && ok "--no-refresh honoured" || bad "--no-refresh honoured"

echo "== T10 clean removes every kind of non-lyric, keeps blank lines =="
mkdir -p "$R/t10"; sine libmp3lame "$R/t10/e.mp3"; tag "$R/t10/e.mp3" Evidence "Some Artist"
cat > "$R/t10/e.lrc" <<'EOF'
[ti:Evidence]
[ar:Some Artist [bracketed (cv.X)]]
[by:site]
[hash:abc123]
[00:00.45]QQ音乐享有本翻译作品的著作权
[00:00.00]Some Artist - Evidence
[00:10.00]Real line

[00:12.00]
[00:14.00]作词：Someone
[00:16.00]Last line
EOF
run "$R/t10" --steps clean --report-dir "$R/reports"
check "exit 0"             "$?" "0"
check "no header tags"     "$(grep -cE '^\[[A-Za-z_]+:' "$R/t10/e.lrc")" "0"
check "bracketed ar gone"  "$(grep -c 'bracketed' "$R/t10/e.lrc")" "0"
check "notice gone"        "$(grep -c '著作权' "$R/t10/e.lrc")" "0"
check "title line gone"    "$(grep -c 'Some Artist - Evidence' "$R/t10/e.lrc")" "0"
check "credit line gone"   "$(grep -c '作词' "$R/t10/e.lrc")" "0"
check "credit in tag"      "$(python3 -c "
from mutagen.id3 import ID3; print(ID3('$R/t10/e.mp3').getall('TEXT')[0].text[0])")" "Someone"
check "lyrics kept"        "$(grep -c '^\[00:10.00\]Real line$' "$R/t10/e.lrc")" "1"
check "last line kept"     "$(grep -c '^\[00:16.00\]Last line$' "$R/t10/e.lrc")" "1"
check "blank line kept"    "$(grep -c '^$' "$R/t10/e.lrc")" "1"
check "timed blank kept"   "$(grep -c '^\[00:12.00\]$' "$R/t10/e.lrc")" "1"

echo "== T11 a blank line does not break the continuation chain =="
mkdir -p "$R/t11"; sine libmp3lame "$R/t11/f.mp3"
printf '[00:10.00]Orig\n译文\n\n[00:20.00]Next\n' > "$R/t11/f.lrc"
run "$R/t11" --steps bilingual --report-dir "$R/reports"
check "adopted across blank" "$(sed -n '2p' "$R/t11/f.lrc")" "[00:10.00]译文"
check "blank preserved"      "$(sed -n '3p' "$R/t11/f.lrc")" ""
check "next untouched"       "$(sed -n '4p' "$R/t11/f.lrc")" "[00:20.00]Next"

echo "== T12 manual/instrumental.txt declaration =="
mkdir -p "$R/t12"; sine libmp3lame "$R/t12/First Light - Camel.mp3"
printf '[00:35.400]Andrew Latimer\n[00:35.790]Guitars Pan\n[00:36.120]Pipes Peter\n' > "$R/t12/First Light - Camel.lrc"
run "$R/t12" --steps clean,nolyrics --report-dir "$R/reports"
check "declared instrumental" "$(cat "$R/t12/First Light - Camel.lrc")" "[00:00.00]No lyrics"

echo "== T13 a non-interactive run refuses to write without --yes =="
mkdir -p "$R/t13"; sine libmp3lame "$R/t13/g.mp3"; printf '[ti:X]\n[00:01.00]L\n' > "$R/t13/g.lrc"
before=$(md5sum "$R/t13/g.lrc" | cut -d' ' -f1)
$ML "$R/t13" --steps clean --no-refresh --report-dir "$R/reports" < /dev/null > "$R/t13.log" 2>&1
check "exit 2"        "$?" "2"
check "refusal shown" "$(grep -c 'Refusing to run' "$R/t13.log")" "1"
check "file untouched" "$(md5sum "$R/t13/g.lrc" | cut -d' ' -f1)" "$before"

echo "== T14 the clean report names every removal and its reason =="
all=$(find "$R/reports" -name clean_removed_report.txt -exec cat {} +)
check "report exists"     "$([ -n "$all" ] && echo yes)" "yes"
check "has header tag"    "$([ "$(printf '%s' "$all" | grep -c '(header tag)')" -gt 0 ] && echo yes)" "yes"
check "has notice reason" "$([ "$(printf '%s' "$all" | grep -c 'source-site notice')" -gt 0 ] && echo yes)" "yes"
check "has title reason"  "$([ "$(printf '%s' "$all" | grep -c '(title line)')" -gt 0 ] && echo yes)" "yes"
check "lists the file"    "$(printf '%s' "$all" | grep -c -- '--- e.lrc')" "1"

echo
echo "================================"
echo " PASS: $pass   FAIL: $fail"
echo "================================"
[ "$fail" -eq 0 ]
