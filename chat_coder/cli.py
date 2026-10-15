"""対話ループ: 指示入力 → pack をクリップボードへ → 応答のコピーを検知 → 反映"""
from __future__ import annotations

import argparse
import difflib
import secrets
import sys
import time
from pathlib import Path

from . import clipboard
from .ignore import LLMIGNORE, ensure_llmignore
from .patch import History, HistoryError, plan_changes
from .project import WARN_CHARS, build_pack, collect_files
from .reply import completed, extract_prose, ingest

COMMANDS = {"/undo", "/undo!", "/redo", "/redo!", "/copy", "/q"}
SHOW_SKIPPED = 15


def beep():
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass


def read_prompt(note):
    print("\n指示を入力(空行で確定)")
    if note:
        print("  ※ 前回の適用エラーを一緒に送ります。空行のみでエラーだけを再送")
    else:
        print("  コマンド: /undo /redo /copy(pack を再コピー) /q  ・何も入力せず空行で終了")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n(入力を取り消しました)")
            return None
        if not line:
            break
        lines.append(line)
        if len(lines) == 1 and line.strip().lower() in COMMANDS:
            break                                 # コマンドは空行を待たずに実行
    return "\n".join(lines)


def wait_reply(pack_id, seq0):
    """応答が揃うまで待って本文を返す。中止したら None"""
    print("チャットの応答をコピーするのを待っています… (Ctrl+C で中止)")
    buf = None
    try:
        while True:
            time.sleep(0.5)
            s = clipboard.seq()
            if s == seq0:
                continue
            seq0 = s
            try:
                text = clipboard.get()
            except OSError:
                continue
            was_none = buf is None
            buf, msg = ingest(text, pack_id, buf)
            if msg:
                print(msg)
            if buf is None:
                continue
            body = completed(buf, pack_id)
            if body is not None:
                return body
            if was_none:
                print(f"応答が途中で切れているようです(末尾の `### END {pack_id}` がありません)。")
                print("チャットで「続き」と送り、続きをコピーしてください。(Ctrl+C でここまでを反映)")
    except KeyboardInterrupt:
        print()
        if buf is not None:
            ans = input("ここまでの内容で反映しますか? [y/N] ").strip().lower()
            if ans == "y":
                return buf
        print("中止しました。")
        return None


def _stat(change):
    a = (change.old_text or "").splitlines()
    b = (change.new_text or "").splitlines()
    add = rem = 0
    for l in difflib.unified_diff(a, b, lineterm="", n=0):
        if l.startswith("+") and not l.startswith("+++"):
            add += 1
        elif l.startswith("-") and not l.startswith("---"):
            rem += 1
    tag = "新規" if change.old_text is None else "削除" if change.new_text is None else "変更"
    return f"  [{tag}] {change.rel}  (+{add} -{rem})"


def _pack(root, instr, note):
    files, skipped = collect_files(root)
    pack_id = secrets.token_hex(3)
    pack = build_pack(files, instr, note, pack_id)
    print(f"{len(files)} ファイルを pack しました。")
    if skipped:
        print(f"  除外 {len(skipped)} 件:")
        for rel, why in skipped[:SHOW_SKIPPED]:
            print(f"    {rel}  ({why})")
        if len(skipped) > SHOW_SKIPPED:
            print(f"    …ほか {len(skipped) - SHOW_SKIPPED} 件")
    return pack, pack_id


def _history_command(history, cmd):
    force = cmd.endswith("!")
    try:
        entry = history.undo(force) if cmd.startswith("/undo") else history.redo(force)
    except HistoryError as e:
        print(f"{e}。")
        if e.conflicts:
            for rel in e.conflicts:
                print(f"  - {rel}")
            print(f"  上書きしてよければ {cmd}! を実行してください。")
        return
    verb = "元に戻しました" if cmd.startswith("/undo") else "やり直しました"
    print(f"{verb}: " + ", ".join(entry.rels))


def _handle_reply(root, history, reply):
    """反映して、次の指示に付けるメモ(失敗時のみ)を返す"""
    prose = extract_prose(reply)
    if prose:
        print("\n--- 応答の説明 ---\n" + prose + "\n------------------")
    plan = plan_changes(root, reply)
    if plan.errors:
        print("適用に失敗しました(何も変更していません):")
        for e in plan.errors:
            print("  -", e)
        print("次の指示を入力すると、このエラーも一緒に送ります(空行のみで再送)。")
        return ("前回の応答は次の理由で適用できませんでした(ファイルは何も変更されていません)。"
                "SEARCH は現在のファイル内容から1文字も変えずにコピーしてやり直してください。\n"
                + "\n".join(f"- {e}" for e in plan.errors))
    if not plan.changes:
        print("変更はありません。" if not plan.n_ops else "変更はありません(内容が同じ)。")
        return ""
    try:
        history.commit(plan.changes)
    except HistoryError as e:
        print(f"反映に失敗しました: {e}")
        return ""
    print("反映しました:")
    for c in plan.changes:
        print(_stat(c))
    print("  (/undo で元に戻せます)")
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chat-coder",
                                 description="チャット経由コーディング用の半自動ループ")
    ap.add_argument("root", nargs="?", default=".", help="プロジェクトのルート(省略時はカレント)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"フォルダが見つかりません: {root}")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    print(f"プロジェクト: {root}")
    if ensure_llmignore(root):
        print(f"除外ルール {LLMIGNORE} を既定の内容で作成しました。送りたくないファイルがあれば編集してください。")
    elif not (root / LLMIGNORE).is_file():
        print(f"※ {LLMIGNORE} を作成できませんでした。内蔵の既定ルールで動作します。")
    history = History(root)
    note = ""
    last = None                                   # (pack, pack_id)
    while True:
        instr = read_prompt(note)
        if instr is None:                         # Ctrl+C: 入力だけ取り消す
            continue
        cmd = instr.strip().lower()
        if cmd == "/q" or (not instr and not note):
            break
        if cmd in COMMANDS and cmd != "/copy":
            _history_command(history, cmd)
            continue
        if cmd == "/copy":
            if last is None:
                print("まだ pack を作っていません。")
                continue
            pack, pack_id = last
        else:
            pack, pack_id = _pack(root, instr or "前回の適用結果を踏まえて修正してください。", note)
            note = ""
            last = (pack, pack_id)
        try:
            clipboard.set(pack)
        except OSError as e:
            print(f"クリップボードに書き込めません: {e}")
            continue
        seq0 = clipboard.seq()
        print(f"クリップボードにコピーしました({len(pack):,} 文字)。チャットに貼り付けて送信してください。")
        if len(pack) > WARN_CHARS:
            print("  ※ 文字数が多いです。チャットの入力上限に注意(超える場合はファイルを絞るか添付で渡す)。")
        reply = wait_reply(pack_id, seq0)
        if reply is None:
            continue
        beep()
        note = _handle_reply(root, history, reply)


if __name__ == "__main__":
    main()
