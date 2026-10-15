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

EDIT_RE = re.compile(
    r"^### FILE:[ \t]*([^\n]+?)[ \t]*\n<<<<<<< SEARCH[ \t]*\n(.*?)^=======[ \t]*\n(.*?)^>>>>>>> REPLACE[ \t]*$",
    re.S | re.M)
DEL_RE = re.compile(r"^### DELETE:[ \t]*([^\n]+?)[ \t]*$", re.M)
SEARCH_RE = re.compile(r"^<<<<<<< SEARCH[ \t]*$", re.M)


@dataclass
class Change:
    rel: str
    old_text: str | None      # None = 存在しなかった
    new_text: str | None      # None = 削除
    old_bytes: bytes | None
    new_bytes: bytes | None


@dataclass
class Plan:
    changes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    n_ops: int = 0            # 応答に含まれていた編集/削除ブロックの数


@dataclass
class _File:
    raw: bytes | None
    text: str | None
    enc: str = "utf-8"
    crlf: bool = False


def _clean_path(s):
    return s.strip().strip("`\"'").replace("\\", "/")


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
    for name in _PROTECTED:                      # 短い名前(GIT~1 など)による別名も防ぐ
        guarded = root / name
        for i in range(len(parts)):
            cand = root.joinpath(*parts[: i + 1])
            try:
                if guarded.exists() and cand.exists() and cand.samefile(guarded):
                    raise ValueError("このパスは変更できない")
            except OSError:
                pass
    return "/".join(parts)


def find_replace(text, old, new):
    n = text.count(old)
    if n > 1:
        raise ValueError(f"SEARCH が {n} 箇所に一致(1箇所に絞る必要あり)")
    if n == 1:
        return text.replace(old, new, 1)
    # 行末空白の違いだけ許容するフォールバック
    tl = text.split("\n")
    ol = old.rstrip("\n").split("\n")
    key = [l.rstrip() for l in ol]
    hits = [i for i in range(len(tl) - len(ol) + 1)
            if [l.rstrip() for l in tl[i:i + len(ol)]] == key]
    if len(hits) != 1:
        raise ValueError(f"SEARCH が {len(hits)} 箇所に一致(0なら元コードと違っている)")
    i = hits[0]
    nl = new.rstrip("\n").split("\n") if new.strip("\n") else []
    tl[i:i + len(ol)] = nl
    return "\n".join(tl)


def parse_ops(reply):
    """[(位置, 'edit'|'delete', 相対パス, SEARCH, REPLACE)] を出現順に返す"""
    ops = []
    for m in EDIT_RE.finditer(reply):
        ops.append((m.start(), "edit", _clean_path(m.group(1)), m.group(2), m.group(3)))
    for m in DEL_RE.finditer(reply):
        ops.append((m.start(), "delete", _clean_path(m.group(1)), None, None))
    ops.sort(key=lambda o: o[0])
    return ops


def plan_changes(root, reply):
    """応答を解釈して変更内容を作る。ファイルは書き換えない。errors があれば適用してはいけない。"""
    reply = reply.replace("\r\n", "\n")
    ops = parse_ops(reply)
    plan = Plan(n_ops=len(ops))
    errors = plan.errors

    def err(msg):
        if msg not in errors:
            errors.append(msg)

    n_search = len(SEARCH_RE.findall(reply))
    n_edit = sum(1 for o in ops if o[1] == "edit")
    if n_search != n_edit:
        err(f"SEARCH が {n_search} 個あるのに {n_edit} 個しか認識できなかった"
            "(ブロックの形式が崩れているか、応答が途中で切れている)")

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

    for _, kind, rel, old, new in ops:
        try:
            rel = _canonical_rel(root, rel)
            touch(rel)
            if kind == "delete":
                if state[rel] is None:
                    raise ValueError("削除対象のファイルが存在しない")
                state[rel] = None
            elif old.strip() == "":
                state[rel] = new                      # 新規作成 / 全体置換
            elif state[rel] is None:
                raise ValueError("編集対象のファイルが存在しない")
            else:
                state[rel] = find_replace(state[rel], old, new)
        except (ValueError, OSError) as e:
            err(f"{rel}: {e}")
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
        if not ignore.exists():                       # バックアップを git の対象にしない
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
            raise HistoryError(f"バックアップを作成できませんでした({e})。ファイルは変更していません")

        # 2. 適用する。失敗したら、それまでに適用したファイルを逆順に戻す
        done = []
        try:
            for c in changes:
                p = self.root / c.rel
                if c.new_bytes is None:
                    p.unlink()
                else:
                    _put_atomic(p, c.new_bytes)
                done.append(c)
        except BaseException as e:
            failed = self._rollback(done)
            if failed:
                raise HistoryError(
                    f"適用の途中で失敗し、次のファイルを元に戻せませんでした: {', '.join(failed)}。"
                    f"変更前の内容は {bdir / 'before'} にあります。手動で復元してください({e})")
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
        return self._move(self._redo, self._undo, "before", "after", force, "やり直す履歴がありません")

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
        for rel in entry.rels:
            p = self.root / rel
            if rel in present:
                _put(p, (entry.dir / restore / rel).read_bytes())
            else:
                p.unlink(missing_ok=True)
        src.pop()
        dst.append(entry)
        return entry
