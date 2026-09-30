#!/usr/bin/env bash
# Regression suite for music_lib.py. Builds a synthetic library under tests/regress/
# and never touches ~/Music.
cd "$(dirname "$0")/.." || exit 1   # musicManage/
S=tests
R=$S/regress
ML=./music_lib.py
pass=0; fail=0

ok()   { pass=$((pass+1)); echo "  PASS  $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL  $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$3] got [$2])"; fi; }

rm -rf "$R"; mkdir -p "$R/reports"
sine() { ffmpeg -hide_banner -loglevel error -f lavfi -i "sine=frequency=440:duration=3" -map_metadata -1 -c:a "$1" "${@:2}"; }

echo "== T1 bilingual keeps untimed + out-of-range lines =="
mkdir -p "$R/t1"; sine libmp3lame "$R/t1/a.mp3"
cat > "$R/t1/a.lrc" <<'EOF'
[ti:T1]
[00:01.00]Orig
[00:04.00]Other
[00:01.00]译文
Follow us: example.com
[00:75.00]Seconds out of range
EOF
$ML "$R/t1" --steps bilingual --no-refresh --report-dir "$R/reports" >/dev/null 2>&1
check "exit 0" "$?" "0"
check "untimed line kept"   "$(grep -c '^Follow us: example.com$' "$R/t1/a.lrc")" "1"
check "out-of-range kept"   "$(grep -c '^\[00:75.00\]Seconds out of range$' "$R/t1/a.lrc")" "1"
check "group regrouped"     "$(sed -n '3p' "$R/t1/a.lrc")" "[00:01.00]译文"

echo "== T2 romaji keeps untimed + out-of-range lines =="
mkdir -p "$R/t2"; sine libmp3lame "$R/t2/a.mp3"
cat > "$R/t2/a.lrc" <<'EOF'
[ti:T2]
[00:01.00]Orig
[00:01.00]译文
[00:01.00]Romaji
Follow us: example.com
[00:75.00]Seconds out of range
[by:someone]
EOF
$ML "$R/t2" --steps romaji --no-refresh --report-dir "$R/reports" >/dev/null 2>&1
check "exit 0" "$?" "0"
check "untimed line kept"  "$(grep -c '^Follow us: example.com$' "$R/t2/a.lrc")" "1"
check "out-of-range kept"  "$(grep -c '^\[00:75.00\]Seconds out of range$' "$R/t2/a.lrc")" "1"
check "late header kept"   "$(grep -c '^\[by:someone\]$' "$R/t2/a.lrc")" "1"
check "orig kept first"    "$(sed -n '2p' "$R/t2/a.lrc")" "[00:01.00]Orig"
check "romaji before zh"   "$(sed -n '3p' "$R/t2/a.lrc")" "[00:01.00]Romaji"
check "zh last"            "$(sed -n '4p' "$R/t2/a.lrc")" "[00:01.00]译文"

echo "== T3 nested .lrc is now seen by credits =="
mkdir -p "$R/t3/deep/deeper"
sine libmp3lame "$R/t3/deep/deeper/song.mp3"
python3 -c "
from mutagen.id3 import ID3, USLT
t=ID3(); t.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]作词：嵌套作者\n[00:02.00]Real lyric')); t.save('$R/t3/deep/deeper/song.mp3')"
$ML "$R/t3" --steps extract,credits --no-refresh --report-dir "$R/reports" > "$R/t3.log" 2>&1
check "nested lrc found"  "$(grep -c 'Found 1 .lrc files' "$R/t3.log")" "1"
check "credit stripped"   "$(grep -c '作词' "$R/t3/deep/deeper/song.lrc")" "0"
check "credit in tag"     "$(python3 -c "
from mutagen.id3 import ID3; print(len(ID3('$R/t3/deep/deeper/song.mp3').getall('TEXT')))")" "1"

echo "== T4 one unwritable file does not abort the run =="
mkdir -p "$R/t4"; sine libmp3lame "$R/t4/b.mp3"
cat > "$R/t4/b.lrc" <<'EOF'
[00:01.00]One
[00:03.00]Three
[00:01.00]Two
EOF
chmod 444 "$R/t4/b.lrc"
$ML "$R/t4" --steps bilingual,romaji --no-refresh --report-dir "$R/reports" > "$R/t4.log" 2> "$R/t4.err"
rc=$?
chmod 644 "$R/t4/b.lrc"
check "exit 1"                 "$rc" "1"
check "error reported"         "$(grep -c 'cannot write' "$R/t4.log")" "1"
check "romaji still ran"       "$(grep -c '=====> \[romaji\]' "$R/t4.log")" "1"
check "abort wording absent"   "$(grep -c 'Run aborted' "$R/t4.log")" "0"

echo "== T5 --max-lines drops are reported =="
mkdir -p "$R/t5"; sine libmp3lame "$R/t5/c.mp3"
printf '[00:01.00]L1\n[00:01.00]L2\n[00:01.00]L3\n[00:01.00]L4\n' > "$R/t5/c.lrc"
$ML "$R/t5" --steps bilingual --max-lines 2 --no-refresh --report-dir "$R/reports" > "$R/t5.log" 2>&1
check "truncation counted" "$(grep -c 'Lines dropped by --max-lines:   2' "$R/t5.log")" "1"
check "note printed"       "$(grep -c 'note: --max-lines dropped' "$R/t5.log")" "1"

echo "== T6 nolyrics keeps .lrc header when audio has no tags =="
mkdir -p "$R/t6"; sine flac "$R/t6/i.flac"
printf '[ti:Keep Me]\n[ar:Keep Artist]\n[00:00.00]纯音乐，请欣赏\n' > "$R/t6/i.lrc"
$ML "$R/t6" --steps nolyrics --no-refresh --report-dir "$R/reports" >/dev/null 2>&1
check "ti kept" "$(grep -c '^\[ti:Keep Me\]$' "$R/t6/i.lrc")" "1"
check "ar kept" "$(grep -c '^\[ar:Keep Artist\]$' "$R/t6/i.lrc")" "1"
$ML "$R/t6" --steps nolyrics --no-refresh --report-dir "$R/reports" > "$R/t6.log" 2>&1
check "idempotent" "$(grep -cE 'Skipped \(already correct\): +1$' "$R/t6.log")" "1"

echo "== T7 dry-run writes nothing but announces reports =="
mkdir -p "$R/t7"; sine libmp3lame "$R/t7/d.mp3"
python3 -c "
from mutagen.id3 import ID3, TIT2
t=ID3(); t.add(TIT2(encoding=3, text=['D'])); t.save('$R/t7/d.mp3')"
before=$(python3 "$S/hash.py" "$R/t7")
mkdir -p "$R/reports7"
$ML "$R/t7" --dry-run --report-dir "$R/reports7" > "$R/t7.log" 2>&1
after=$(python3 "$S/hash.py" "$R/t7")
check "no writes"          "$([ "$before" = "$after" ] && echo same)" "same"
check "no lrc created"     "$([ -f "$R/t7/d.lrc" ] && echo yes || echo no)" "no"
check "report announced"   "$(grep -c 'Would write report lrc_missing_report.txt' "$R/t7.log")" "1"
check "no report file"     "$(ls "$R/reports7" | wc -l)" "0"

echo "== T8 full pipeline is idempotent =="
mkdir -p "$R/t8"; sine libvorbis "$R/t8/e.ogg"; sine libmp3lame "$R/t8/f.mp3"
python3 -c "
from mutagen.id3 import ID3, USLT
t=ID3(); t.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]作词：A\n[00:03.00]Real line')); t.save('$R/t8/f.mp3')"
$ML "$R/t8" --no-refresh --report-dir "$R/reports" > "$R/t8.1.log" 2>&1
h1=$(python3 "$S/hash.py" "$R/t8")
$ML "$R/t8" --no-refresh --report-dir "$R/reports" > "$R/t8.2.log" 2>&1
h2=$(python3 "$S/hash.py" "$R/t8")
check "first run wrote"  "$(grep -c 'Wrote title:                    1' "$R/t8.1.log")" "1"
check "second run no-op" "$(grep -c 'Updated:                        0' "$R/t8.2.log")" "2"
check "bytes stable"     "$([ "$h1" = "$h2" ] && echo same)" "same"

echo "== T9 refresh policy =="
$ML "$R/t8" --steps ogg --dry-run --report-dir "$R/reports" 2>&1 | grep -q "skipped in dry-run" && ok "dry-run skips refresh" || bad "dry-run skips refresh"
$ML "$R/t8" --steps ogg --no-refresh --report-dir "$R/reports" 2>&1 | grep -q "skipped: --no-refresh" && ok "--no-refresh honoured" || bad "--no-refresh honoured"

echo
echo "================================"
echo " PASS: $pass   FAIL: $fail"
echo "================================"
[ "$fail" -eq 0 ]
