"""プロジェクトのファイル収集と、チャットに送る pack の生成"""

from __future__ import annotations

import codecs
import os
import subprocess
from pathlib import Path

from .ignore import IgnoreMatcher

MAX_FILE_BYTES = 200_000
WARN_CHARS = 150_000

PACK_MARK = "# LLM_LOOP PACK"  # pack の先頭行。応答と取り違えないための目印

RULES = (
    PACK_MARK
    + """
あなたはコーディングアシスタントです。下記のプロジェクト全ファイルと指示を渡します。

【出力ルール】
- 応答全体を、バッククォート4つ(````)のコードブロックで囲む。
- コードブロックの先頭行は `### LLM_LOOP {id}`、最終行は `### END {id}` とする(この2行は必ず書く)。
  {id} はこの送信で渡した値だけを使う。会話の前の応答に書いた id は無効なので、真似て書かない。
- 変更は下記の形式のブロックで出力し、説明は最小限にする。質問への回答だけを返す場合も、先頭行と最終行は書く。
- 既存ファイルの編集(記号はちょうど7文字。ブロックごとに `### FILE:` の行から書く):
### FILE: 相対パス
<<<<<<< SEARCH
(元のコードを1文字も変えずにコピー。空白・インデントも含めて同じにする)
=======
(置換後のコード)
>>>>>>> REPLACE
- SEARCH は、ファイル内で1箇所だけに一致する最小限の長さにする(変更する行と、その前後1〜2行が目安)。
  変更しない行を長々とコピーしない。REPLACE にも、変更に関係ない行は含めない。
- 長い範囲を置き換えるときは、SEARCH を「先頭の2〜3行」「`...` だけの行」「末尾の2〜3行」にしてよい。
  先頭部分から、その後に最初に現れる末尾部分までが、すべて REPLACE の内容に置き換わる。
- 新規作成・ファイル全体の置換: SEARCH を空にする(<<<<<<< SEARCH の直後の行を ======= にする)。
- ファイル削除: ### DELETE: 相対パス
- 同じファイルに複数ブロックを書いてよい(上から順に適用される。後のブロックの SEARCH は、前のブロックを適用した後の内容に対して照合される)。
- パスはプロジェクトルートからの相対パス(/区切り)。
- 出力が長くて途中で切れ、ユーザーから「続き」と言われたら、最後に出力した行を先頭から書き直し、その続きから出力する。最終行の `### END {id}` は最後に1回だけ書く。"""
)

RULES_TAIL = (
    "# 応答の書式(再掲)\n"
    "- 先頭行 `### LLM_LOOP {id}`、最終行 `### END {id}`。{id} はこの送信の値で、前の応答の id は使わない。\n"
    "- SEARCH は元コードのコピー(1箇所に一致する最小限)。適用に失敗した場合は、上の「前回の適用結果」に理由がある。"
)


def list_paths(root, matcher=None):
    """送る候補のファイル(ルート相対・/区切り)。既定ルール・.gitignore・.llmignore で除外済み"""
    matcher = matcher or IgnoreMatcher(root)
    try:
        r = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        paths = [p for p in r.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        paths = []
        for d, dirs, files in os.walk(root):
            rel_d = Path(d).relative_to(root).as_posix()
            rel_d = "" if rel_d == "." else rel_d + "/"
            dirs[:] = [x for x in dirs if not matcher.is_ignored(rel_d + x, is_dir=True)]
            paths += [rel_d + f for f in files]
    return [p for p in sorted(set(paths)) if not matcher.is_ignored(p)]


def decode_bytes(raw):
    """(改行を \\n に正規化した文字列, エンコーディング名)。扱えなければ ValueError(理由を含む)"""
    if b"\0" in raw:
        raise ValueError("バイナリ")
    if raw.startswith(codecs.BOM_UTF8):
        try:
            return raw[3:].decode("utf-8").replace("\r\n", "\n"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc).replace("\r\n", "\n"), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判別できない(UTF-8 / cp932 以外)")


def _inside(root, path):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def collect_files(root):
    """([(相対パス, 本文)], [(相対パス, 除外理由)])"""
    files, skipped = [], []
    for rel in list_paths(root):
        p = root / rel
        try:
            if not p.is_file():
                continue
            if p.is_symlink() and not _inside(root, p):
                skipped.append((rel, "プロジェクト外を指すリンク"))
                continue
            size = p.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped.append((rel, f"{size // 1000}KB 超"))
                continue
            text, _ = decode_bytes(p.read_bytes())
        except ValueError as e:
            skipped.append((rel, str(e)))
            continue
        except OSError as e:
            skipped.append((rel, f"読めない: {e}"))
            continue
        files.append((rel, text))
    return files, skipped


def build_pack(files, instruction, note, pack_id):
    parts = [RULES.replace("{id}", pack_id), ""]
    if note:
        parts += ["# 前回の適用結果", note, ""]
    parts += ["# 指示", instruction, "", "# ファイル一覧"]
    parts += [rel for rel, _ in files]
    parts.append("")
    for rel, text in files:
        parts.append(f"##### BEGIN FILE: {rel}")
        parts.append(text if text.endswith("\n") or not text else text + "\n")
        parts.append("##### END FILE")
    parts += ["", RULES_TAIL.replace("{id}", pack_id)]
    return "\n".join(parts) + "\n"
