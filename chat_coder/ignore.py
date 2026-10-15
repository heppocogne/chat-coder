"""gitignore 形式の除外ルール(標準ライブラリのみ。基本的なパターンに対応)

対応: `#` コメント / `!` 否定 / 末尾 `/`(ディレクトリのみ) / 先頭 `/` や途中の `/`(基準ディレクトリ固定) /
      `*` `?` `[...]` / `**` / `\\` エスケープ / サブディレクトリの .gitignore
未対応: .git/info/exclude とグローバル設定(git 管理下では git 自身が処理する)

ルールの出どころ: 各階層の .gitignore と、ルートの .llmignore(無ければ既定の内容で自動作成)。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

GITIGNORE = ".gitignore"
LLMIGNORE = ".llmignore"  # プロジェクトルートに置く、このツール専用の除外ルール(最後に評価される)

# 既定の除外ルール(.gitignore と同じ書式)。.llmignore が無いとき、この内容で作成する
DEFAULT_IGNORE = """\
# バージョン管理・このツール自身のバックアップ
.git/
.llm_backup/

# 依存・生成物・環境
node_modules/
__pycache__/
.venv/
venv/
dist/
build/

# エディタ
.idea/
.vscode/

# ロックファイル
package-lock.json
yarn.lock
bun.lock
poetry.lock
pnpm-lock.yaml
uv.lock
"""

MAX_PATTERN_LEN = 256
PROTECTED_DIRS = {".git", ".llm_backup"}  #どのルールでも再包含できない(常に除外)

_LLMIGNORE_HEADER = """\
# chat-coder の除外ルール(.gitignore と同じ書式)。
# チャットに送るファイルを決めます。自由に編集してください。
# - .gitignore の内容もあわせて除外されます。このファイルは .gitignore より後に評価されます
#   (`!パターン` で再包含も可能)。
# - .git/ と .llm_backup/ は、ここに書かなくても常に除外されます。
# - このファイルを消すと、次回の起動で既定の内容から作り直します。

"""

_LLMIGNORE_HINT = """
# 秘密情報(必要なら先頭の # を外す)
.env
# .env.*
*.pem
*.key
id_rsa
"""

_FLAGS = re.IGNORECASE if os.name == "nt" else 0


def ensure_llmignore(root):
    """ルートに .llmignore が無ければ既定のルールで作る。作成したら True"""
    path = Path(root) / LLMIGNORE
    if path.exists():
        return False
    try:
        path.write_bytes(
            (_LLMIGNORE_HEADER + DEFAULT_IGNORE + _LLMIGNORE_HINT).encode("utf-8")
        )
    except OSError:
        return False
    return True


class Rule:
    __slots__ = ("base", "regex", "negate", "dir_only")

    def __init__(self, base, regex, negate, dir_only):
        self.base = base
        self.regex = regex
        self.negate = negate
        self.dir_only = dir_only


def _to_regex(pat):
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            j = i + 2 if pat.startswith("**", i) else i + 1
            at_start = i == 0 or pat[i - 1] == "/"
            if j == i + 2 and at_start and j < n and pat[j] == "/":
                out.append("(?:.*/)?")  # `**/` = 0個以上のディレクトリ
                i = j + 1
                continue
            if j == i + 2 and at_start and j >= n:
                out.append(".*")  # 末尾の `/**` = 配下すべて
                i = j
                continue
            out.append("[^/]*")  # 通常の `*`(名前の途中の `**` もこれ)
            i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def parse(text, base=""):
    """gitignore 形式のテキストを Rule のリストにする。base はそのファイルがあるディレクトリ(ルート相対)"""
    rules = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.endswith("\\ "):
            line = line.rstrip(" ")
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        if len(line) > MAX_PATTERN_LEN:          # 極端に長いパターンは正規表現が重くなるので無視
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        body = _to_regex(line)
        if not anchored:
            body = "(?:.*/)?" + body  # どの階層の名前にも一致
        try:
            rules.append(Rule(base, re.compile(body, _FLAGS), negate, dir_only))
        except re.error:
            continue  # 壊れたパターンは無視
    return rules


def _read(path):
    """ファイルの内容。読めなければ None(呼び出し側で安全な側に倒す)"""
    try:
        return path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return None


class IgnoreMatcher:
    """各階層の .gitignore(浅い順) → ルートの .llmignore の順に評価し、最後に一致したものが勝つ。
    .llmignore が無ければ DEFAULT_IGNORE を代わりに使う(ensure_llmignore で作成しておくのが通常)。
    親ディレクトリが除外されていれば、その配下は(git と同じく)再包含できない。
    PROTECTED_DIRS はルールに関わらず常に除外する。"""

    def __init__(self, root):
        self.root = Path(root)
        path = self.root / LLMIGNORE
        # .llmignore が無い・読めない場合は既定ルールで動く(除外ルールが空になる fail-open を避ける)
        text = _read(path) if path.is_file() else None
        self._llm = parse(text if text is not None else DEFAULT_IGNORE)
        self._dirs = {}

    def _gitignore(self, base):
        if base not in self._dirs:
            d = self.root.joinpath(*base.split("/")) if base else self.root
            self._dirs[base] = parse(_read(d / GITIGNORE) or "", base)
        return self._dirs[base]

    def is_ignored(self, rel, is_dir=False):
        parts = rel.strip("/").split("/")
        if any(p.rstrip(" .").lower() in PROTECTED_DIRS for p in parts):
            return True
        for i in range(len(parts)):
            last = i == len(parts) - 1
            if self._match(
                "/".join(parts[: i + 1]), parts[:i], is_dir if last else True
            ):
                return True
        return False

    def _match(self, path, parents, is_dir):
        rules = []
        for k in range(len(parents) + 1):
            rules += self._gitignore("/".join(parents[:k]))
        rules += self._llm
        ignored = False
        for r in rules:
            if r.dir_only and not is_dir:
                continue
            if r.base:
                if not path.startswith(r.base + "/"):
                    continue
                sub = path[len(r.base) + 1 :]
            else:
                sub = path
            if r.regex.fullmatch(sub):
                ignored = not r.negate
        return ignored
