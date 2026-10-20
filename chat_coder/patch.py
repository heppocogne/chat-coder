"""応答の SEARCH/REPLACE ブロックの解析・適用と、元に戻す履歴"""

from __future__ import annotations

import datetime
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .project import decode_bytes

BACKUP_DIRNAME = ".llm_backup"

# 行単位で解釈する。モデルが崩しがちな点(マーカーの記号数・大文字小文字・インデント、
# FILE 行と SEARCH 行の間の空行やコードフェンス)は許容する
FILE_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?FILE:(?:\*\*)?[ \t]*(.+?)[ \t]*$", re.I)
DEL_RE = re.compile(r"^### DELETE:[ \t]*(.+?)[ \t]*$")
SEARCH_RE = re.compile(r"^([ \t]*)<{5,9}[ \t]*SEARCH[ \t]*$", re.I)
DIVIDER_RE = re.compile(r"^[ \t]*={5,9}[ \t]*$")
REPLACE_RE = re.compile(r"^[ \t]*>{5,9}[ \t]*REPLACE[ \t]*$", re.I)
GAP_RE = re.compile(r"^[ \t]*(?:`{3,}.*)?$")  # 空行・コードフェンス行(ブロック間に挟まってもよい)
ELLIPSIS = ("...", "…", "⋮")  # SEARCH 中でこれだけの行は「間を省略」の意味
MAX_ELIDED_LINES = 400


@dataclass
class Op:
    kind: str  # 'edit' | 'delete'
    rel: str
    old: str | None
    new: str | None
    start: int  # 応答中の開始行・終了行(0始まり、終了は含む)
    end: int
    index: int = 0  # 何番目の編集ブロックか(1始まり)

    @property
    def where(self):
        if self.kind == "delete":
            return f"応答の{self.start + 1}行目"
        return f"ブロック{self.index}・応答の{self.start + 1}行目"


@dataclass
class Change:
    rel: str
    old_text: str | None  # None = 存在しなかった
    new_text: str | None  # None = 削除
    old_bytes: bytes | None
    new_bytes: bytes | None


@dataclass
class Plan:
    changes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    n_ops: int = 0  # 応答に含まれていた編集/削除ブロックの数


@dataclass
class _File:
    raw: bytes | None
    text: str | None
    enc: str = "utf-8"
    crlf: bool = False


def _clean_path(s):
    return s.strip().strip("`\"'*").replace("\\", "/")


_PROTECTED = (".git", BACKUP_DIRNAME)


def _canonical_rel(root, rel):
    """応答中のパスを、解決後のルート相対パス(/区切り)に正規化する。許可できなければ ValueError"""
    if not rel.strip():
        raise ValueError("パスが空")
    # resolve() の前に拒否する(UNC パス \\host\share を解決するとネットワークに接続してしまう)
    pw = PureWindowsPath(rel)
    if pw.drive or pw.root or rel.startswith(("/", "\\")) or ":" in rel:
        raise ValueError("絶対パス・UNC パス・ドライブ指定は使えない")
    target = (root / rel).resolve()
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise ValueError("プロジェクトの外を指している")
    if not parts:
        raise ValueError("パスがプロジェクトルート自身")
    # 解決後のどの階層も .git / .llm_backup であってはならない(大文字小文字・末尾の空白やドットも同一視)
    if any(p.rstrip(" .").lower() in _PROTECTED for p in parts):
        raise ValueError("このパスは変更できない")
    for name in _PROTECTED:  # 短い名前(GIT~1 など)による別名も防ぐ
        guarded = root / name
        for i in range(len(parts)):
            cand = root.joinpath(*parts[: i + 1])
            try:
                if guarded.exists() and cand.exists() and cand.samefile(guarded):
                    raise ValueError("このパスは変更できない")
            except OSError:
                pass
    return "/".join(parts)


def _lines_eq(a, b):
    return [l.rstrip() for l in a] == [l.rstrip() for l in b]


def _find_exact(tl, ol):
    return [i for i in range(len(tl) - len(ol) + 1) if _lines_eq(tl[i : i + len(ol)], ol)]


def _split_elided(ol):
    """SEARCH の行を、`...` だけの行で (先頭部分, 末尾部分) に分ける。省略が無ければ None"""
    idx = [i for i, l in enumerate(ol) if l.strip() in ELLIPSIS]
    if len(idx) != 1:
        return None
    head, tail = ol[: idx[0]], ol[idx[0] + 1 :]
    return (head, tail) if head and tail else None


def _find_elided(tl, head, tail):
    """先頭部分で始まり、その後で最初に現れる末尾部分で終わる範囲 [(開始, 終了の次)]"""
    out = []
    for i in range(len(tl) - len(head) + 1):
        if not _lines_eq(tl[i : i + len(head)], head):
            continue
        lo = i + len(head)
        for j in range(lo, min(len(tl) - len(tail), lo + MAX_ELIDED_LINES) + 1):
            if _lines_eq(tl[j : j + len(tail)], tail):
                out.append((i, j + len(tail)))
                break
    return out


def _show(line, width=70):
    line = line.rstrip("\n")
    return repr(line if len(line) <= width else line[:width] + "…")


def _explain_mismatch(tl, ol):
    """SEARCH が元コードのどこと食い違っているかを説明する文"""
    nonblank = [l for l in ol if l.strip()]
    if not nonblank:
        return "SEARCH が空"
    # 空白(インデント)を無視すれば一致するか
    stripped = [l.strip() for l in ol]
    n = len(ol)
    for i in range(len(tl) - n + 1):
        if [l.strip() for l in tl[i : i + n]] == stripped:
            return (
                f"インデント(先頭の空白・タブ)だけが違う。元コードの {i + 1} 行目付近は "
                f"{_show(tl[i])} だが、SEARCH は {_show(ol[0])}"
            )
    # 最も多くの行が一致する位置を探す
    best, best_i = -1, 0
    for i in range(max(1, len(tl) - n + 1)):
        score = sum(1 for k in range(min(n, len(tl) - i)) if tl[i + k].rstrip() == ol[k].rstrip())
        if score > best:
            best, best_i = score, i
    if best <= 0:
        # 先頭の1行だけでも探す
        first = nonblank[0].strip()
        hits = [i for i, l in enumerate(tl) if l.strip() == first]
        if hits:
            best_i = hits[0]
        else:
            return (
                f"元コードに存在しない行を含む(例: {_show(nonblank[0])})。"
                "別のファイルのコードか、存在しない関数・行を書いている可能性"
            )
    for k in range(n):
        have = tl[best_i + k] if best_i + k < len(tl) else None
        if have is None or have.rstrip() != ol[k].rstrip():
            return (
                f"最も近い箇所は元コードの {best_i + 1} 行目付近。"
                f"SEARCH の {k + 1} 行目が違う: SEARCH={_show(ol[k])} / "
                f"元コード={_show(have) if have is not None else '(ファイル末尾を超えた)'}"
            )
    return "原因を特定できなかった"


def find_replace(text, old, new):
    n = text.count(old)
    if n > 1:
        tl0 = text.split("\n")
        ol0 = old.rstrip("\n").split("\n")
        at = ", ".join(str(i + 1) for i in _find_exact(tl0, ol0)[:6])
        raise ValueError(
            f"SEARCH が {n} 箇所に一致(行 {at})。前後の行を足して1箇所に絞る必要がある"
        )
    if n == 1:
        return text.replace(old, new, 1)
    tl = text.split("\n")
    ol = old.rstrip("\n").split("\n")
    nl = new.rstrip("\n").split("\n") if new.strip("\n") else []
    # 行末空白の違いだけ許容するフォールバック
    hits = [(i, i + len(ol)) for i in _find_exact(tl, ol)]
    # `...` で間を省略した SEARCH
    elided = _split_elided(ol)
    if not hits and elided:
        hits = _find_elided(tl, *elided)
        if not hits:
            raise ValueError(
                "`...` を含む SEARCH の先頭部分と末尾部分に一致する範囲が元コードに無い。"
                + _explain_mismatch(tl, elided[0])
            )
    if len(hits) > 1:
        at = ", ".join(str(i + 1) for i, _ in hits[:6])
        raise ValueError(f"SEARCH が {len(hits)} 箇所に一致(行 {at})。1箇所に絞る必要がある")
    if not hits:
        raise ValueError("SEARCH が元コードと一致しない。" + _explain_mismatch(tl, ol))
    i, j = hits[0]
    tl[i:j] = nl
    return "\n".join(tl)


def _dedent(lines, indent):
    if not indent:
        return lines
    return [l[len(indent) :] if l.startswith(indent) else l for l in lines]


def parse_ops(reply):
    """応答から編集・削除を出現順に取り出す。(ops, 形式の問題の一覧)"""
    lines = reply.split("\n")
    ops, problems = [], []
    path = None  # (相対パス, FILE 行の位置)。FILE 行のあと、空行・フェンス以外が挟まると無効
    last = None  # 直前のブロックのパス(同じファイルの続きのブロックで FILE 行が省かれた場合用)
    n_edit = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = DEL_RE.match(ln)
        if m:
            ops.append(Op("delete", _clean_path(m.group(1)), None, None, i, i))
            path = last = None
            i += 1
            continue
        m = FILE_RE.match(ln)
        if m:
            path = (_clean_path(m.group(1)), i)
            i += 1
            continue
        m = SEARCH_RE.match(ln)
        if not m:
            if REPLACE_RE.match(ln):
                problems.append(f"応答の{i + 1}行目: 対応する SEARCH のない `>>>>>>> REPLACE`")
            if not GAP_RE.match(ln):
                path = last = None
            i += 1
            continue
        # ---- SEARCH ブロック
        start, indent = i, m.group(1)
        who = f"応答の{start + 1}行目の SEARCH"
        search, replace, j = [], [], i + 1
        while j < len(lines) and not DIVIDER_RE.match(lines[j]):
            if SEARCH_RE.match(lines[j]):
                break
            search.append(lines[j])
            j += 1
        if j >= len(lines) or SEARCH_RE.match(lines[j]):
            at = "" if j >= len(lines) else f"(応答の{j + 1}行目で次の SEARCH が始まっている)"
            problems.append(
                f"{who}: 区切りの `=======` が無い{at}"
                + ("。応答が途中で切れている可能性" if j >= len(lines) else "")
            )
            path = last = None
            i = j
            continue
        j += 1
        while j < len(lines) and not REPLACE_RE.match(lines[j]):
            if SEARCH_RE.match(lines[j]):
                break
            replace.append(lines[j])
            j += 1
        if j >= len(lines) or SEARCH_RE.match(lines[j]):
            at = "" if j >= len(lines) else f"(応答の{j + 1}行目で次の SEARCH が始まっている)"
            problems.append(
                f"{who}: 終端の `>>>>>>> REPLACE` が無い{at}"
                + ("。応答が途中で切れている可能性" if j >= len(lines) else "")
            )
            path = last = None
            i = j
            continue
        if path:
            rel, first = path[0], path[1]
        elif last:
            rel, first = last, start
        else:
            rel = None
        if rel is None:
            problems.append(
                f"{who}: 直前に `### FILE: パス` の行が無く、対象のファイルが分からない"
            )
        else:
            n_edit += 1
            search, replace = _dedent(search, indent), _dedent(replace, indent)
            ops.append(
                Op(
                    "edit",
                    rel,
                    "\n".join(search) + "\n" if search else "",
                    "\n".join(replace) + "\n" if replace else "",
                    first,
                    j,
                    n_edit,
                )
            )
            last = rel
        path = None
        i = j + 1
    return ops, problems


def strip_blocks(reply):
    """編集・削除のブロックを取り除いた応答(説明文の抽出用)"""
    reply = reply.replace("\r\n", "\n")
    ops, _ = parse_ops(reply)
    drop = set()
    for op in ops:
        drop.update(range(op.start, op.end + 1))
    return "\n".join(l for k, l in enumerate(reply.split("\n")) if k not in drop)


def plan_changes(root, reply):
    """応答を解釈して変更内容を作る。ファイルは書き換えない。errors があれば適用してはいけない。"""
    reply = reply.replace("\r\n", "\n")
    ops, problems = parse_ops(reply)
    plan = Plan(n_ops=len(ops))
    errors = plan.errors

    def err(msg):
        if msg not in errors:
            errors.append(msg)

    for msg in problems:
        err("[形式] " + msg)

    files, state = {}, {}

    def touch(rel):
        if rel in files:
            return
        p = root / rel
        if p.is_file():
            raw = p.read_bytes()
            text, enc = decode_bytes(raw)
            files[rel] = _File(raw, text, enc, b"\r\n" in raw)
        else:
            files[rel] = _File(None, None)
        state[rel] = files[rel].text

    for op in ops:
        kind, old, new = op.kind, op.old, op.new
        rel = op.rel
        try:
            rel = _canonical_rel(root, rel)
            touch(rel)
            if kind == "delete":
                if state[rel] is None:
                    raise ValueError("削除対象のファイルが存在しない")
                state[rel] = None
            elif old.strip() == "":
                state[rel] = new  # 新規作成 / 全体置換
            elif state[rel] is None:
                raise ValueError("編集対象のファイルが存在しない")
            else:
                state[rel] = find_replace(state[rel], old, new)
        except (ValueError, OSError) as e:
            kind_label = "[削除]" if kind == "delete" else "[適用]"
            err(f"{kind_label} {rel}({op.where}): {e}")
    if errors:
        return plan

    for rel, new in state.items():
        f = files[rel]
        if new == f.text:
            continue
        data = None
        if new is not None:
            try:
                data = (new.replace("\n", "\r\n") if f.crlf else new).encode(f.enc)
            except UnicodeEncodeError:
                err(f"{rel}: 元のファイルの文字コード({f.enc})で表現できない文字が含まれている")
                continue
        plan.changes.append(Change(rel, f.text, new, f.raw, data))
    return plan


# ---------------------------------------------------------------- 履歴(undo / redo)
class HistoryError(Exception):
    def __init__(self, message, conflicts=()):
        super().__init__(message)
        self.conflicts = list(conflicts)


@dataclass
class Entry:
    dir: Path
    rels: list
    had_before: set
    has_after: set


class _Conflict(Exception):
    def __init__(self, rel):
        super().__init__(rel)
        self.rel = rel


def _put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _put_atomic(path, data):
    """一時ファイルに書いてから置き換える。途中で失敗しても元のファイルは壊れない"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read(path):
    """ファイルの現在の内容。存在しなければ None"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return b"\0<directory>"  # どの期待値とも一致させない


class History:
    """適用ごとに .llm_backup/<日時>/{before,after}/ を残し、undo / redo できるようにする"""

    def __init__(self, root):
        self.root = root
        self.base = root / BACKUP_DIRNAME
        self._undo = []
        self._redo = []

    def commit(self, changes):
        """変更を適用する。途中で失敗したら、すでに変更したファイルを元に戻してから HistoryError を投げる。
        成功したときだけ undo の履歴に登録する。"""
        self.base.mkdir(exist_ok=True)
        ignore = self.base / ".gitignore"
        if not ignore.exists():  # バックアップを git の対象にしない
            ignore.write_text("*\n", encoding="utf-8")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir, n = self.base / stamp, 1
        while bdir.exists():
            n += 1
            bdir = self.base / f"{stamp}_{n}"
        entry = Entry(bdir, [c.rel for c in changes], set(), set())

        # 1. 変更前・変更後をすべてバックアップする。失敗したら何も変更しない
        try:
            for c in changes:
                if c.old_bytes is not None:
                    _put(bdir / "before" / c.rel, c.old_bytes)
                    entry.had_before.add(c.rel)
                if c.new_bytes is not None:
                    _put(bdir / "after" / c.rel, c.new_bytes)
                    entry.has_after.add(c.rel)
        except OSError as e:
            shutil.rmtree(bdir, ignore_errors=True)
            raise HistoryError(
                f"バックアップを作成できませんでした({e})。ファイルは変更していません"
            )

        # 2. 適用する。失敗したら、それまでに適用したファイルを逆順に戻す
        # 計画作成後に外部で変更されたファイルを上書きしないよう、書き込む直前にファイルごとに再確認する
        done = []
        try:
            for c in changes:
                p = self.root / c.rel
                if _read(p) != c.old_bytes:
                    raise _Conflict(c.rel)
                if c.new_bytes is None:
                    p.unlink()
                else:
                    _put_atomic(p, c.new_bytes)
                done.append(c)
        except BaseException as e:
            failed = self._rollback(done)
            if isinstance(e, _Conflict):
                if not failed:
                    shutil.rmtree(bdir, ignore_errors=True)
                    raise HistoryError(
                        "適用直前にファイルが外部で変更されたため、何も変更せず中止しました",
                        [e.rel],
                    )
                e = f"{e.rel} が外部で変更された"
            if failed:
                raise HistoryError(
                    f"適用の途中で失敗し、次のファイルを元に戻せませんでした: {', '.join(failed)}。"
                    f"変更前の内容は {bdir / 'before'} にあります。手動で復元してください({e})"
                )
            shutil.rmtree(bdir, ignore_errors=True)
            if isinstance(e, OSError):
                raise HistoryError(f"適用の途中で失敗したため、すべて元に戻しました({e})")
            raise

        self._undo.append(entry)
        self._redo.clear()
        return entry

    def _rollback(self, done):
        """適用済みの変更を元に戻す。戻せなかったパスの一覧を返す"""
        failed = []
        for c in reversed(done):
            try:
                if c.old_bytes is None:
                    (self.root / c.rel).unlink(missing_ok=True)
                else:
                    _put_atomic(self.root / c.rel, c.old_bytes)
            except OSError:
                failed.append(c.rel)
        return failed

    def undo(self, force=False):
        return self._move(self._undo, self._redo, "after", "before", force, "戻す履歴がありません")

    def redo(self, force=False):
        return self._move(
            self._redo, self._undo, "before", "after", force, "やり直す履歴がありません"
        )

    def _present(self, entry, state):
        return entry.had_before if state == "before" else entry.has_after

    def _conflicts(self, entry, state):
        """entry が state の状態のはずなのに、手で変更されているファイル"""
        present = self._present(entry, state)
        out = []
        for rel in entry.rels:
            p = self.root / rel
            if rel in present:
                ok = p.is_file() and p.read_bytes() == (entry.dir / state / rel).read_bytes()
            else:
                ok = not p.exists()
            if not ok:
                out.append(rel)
        return out

    def _move(self, src, dst, expect, restore, force, empty_msg):
        if not src:
            raise HistoryError(empty_msg)
        entry = src[-1]
        if not force:
            conflicts = self._conflicts(entry, expect)
            if conflicts:
                raise HistoryError("適用後に手で変更されたファイルがあります", conflicts)
        present = self._present(entry, restore)
        expected = self._present(entry, expect)
        # 確認から書き込みまでの間に変更されても気付けるよう、書き込む直前にファイルごとに再確認する。
        # force のときは上書きするが、失われる内容は entry.dir/overwritten に退避する。
        done = []  # (path, 書き込む前の内容)
        try:
            for rel in entry.rels:
                p = self.root / rel
                cur = _read(p)
                want = (entry.dir / expect / rel).read_bytes() if rel in expected else None
                if cur != want:
                    if not force:
                        raise _Conflict(rel)
                    if cur is not None:
                        _put(entry.dir / "overwritten" / rel, cur)
                if rel in present:
                    _put_atomic(p, (entry.dir / restore / rel).read_bytes())
                else:
                    p.unlink(missing_ok=True)
                done.append((p, cur))
        except BaseException as e:
            failed = []
            for p, cur in reversed(done):
                try:
                    if cur is None:
                        p.unlink(missing_ok=True)
                    else:
                        _put_atomic(p, cur)
                except OSError:
                    failed.append(str(p.relative_to(self.root)))
            if failed:
                raise HistoryError(
                    f"途中で失敗し、次のファイルを元に戻せませんでした: {', '.join(failed)}"
                )
            if isinstance(e, _Conflict):
                raise HistoryError(
                    "確認後にファイルが外部で変更されたため、何も変更せず中止しました", [e.rel]
                )
            if isinstance(e, OSError):
                raise HistoryError(f"途中で失敗したため、すべて元に戻しました({e})")
            raise
        src.pop()
        dst.append(entry)
        return entry
