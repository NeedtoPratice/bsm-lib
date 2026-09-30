# music_lib.py 检查用临时目录（`.gitignore` 已忽略 `.tmp_*/`）

本目录是 2026-09-28 对 `../../music_lib.py` 的检查/回归工具，可随时整目录删除。

- `regress.sh` —— 32 项回归套件。自建合成曲库（用 ffmpeg 生成 ogg/mp3/flac），
  覆盖静默丢行、子目录 `.lrc`、只读文件、`--max-lines`、`nolyrics` 头部保留、
  dry-run 零写入、幂等性与刷新策略。运行：`bash regress.sh`，全过时退出码 0。
- `audit.py` —— 对真实曲库 `~/Music` 的**只读**审计：配对情况、丢行风险敞口、
  不动点检查（哪些步骤会改写文件）、同时间戳分组的语言形态统计。
- `hash.py` —— 目录内容 md5 清单，供 dry-run / 幂等性做字节级比对。
- `ab_old.txt` / `ab_new.txt` —— 旧版（`git show HEAD:music_lib.py`）与修复版
  对真实曲库 `--dry-run` 的输出，用于比对逐步计数器。
