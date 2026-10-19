"""対話ループ: 指示入力 → pack をクリップボードへ → 応答のコピーを検知 → 反映"""

from __future__ import annotations

import argparse
import difflib
import secrets
import sys
import time
from pathlib import Path

from . import __version__, autosend, clipboard
from .ignore import LLMIGNORE, ensure_llmignore
from .patch import BACKUP_DIRNAME, History, HistoryError, plan_changes
from .project import WARN_CHARS, build_pack, collect_files
from .reply import completed, extract_prose, ingest

COMMANDS = {"/undo", "/undo!", "/redo", "/redo!", "/copy", "/wait", "/q"}
CTRL_Q = "\x11"
SHOW_SKIPPED = 15


def beep():
    try:
        import winsound

        winsound.MessageBeep()
    except Exception:
        pass


class _KeyPoller:
    """待機中のキー入力(Ctrl+Q)を非ブロッキングで読む。使えない環境では何も返さない"""

    def __enter__(self):
        self._old = None
        try:
            import msvcrt  # noqa: F401

            self._win = True
        except ImportError:
            self._win = False
            try:
                import termios
                import tty

                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            except Exception:
                self._old = None
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    @property
    def available(self):
        return self._win or self._old is not None

    def pressed(self):
        """溜まっているキーに Ctrl+Q があれば True"""
        try:
            if self._win:
                import msvcrt

                hit = False
                while msvcrt.kbhit():
                    if msvcrt.getwch() == CTRL_Q:
                        hit = True
                return hit
            if self._old is not None:
                import select

                hit = False
                while select.select([sys.stdin], [], [], 0)[0]:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                    if ch == CTRL_Q:
                        hit = True
                return hit
        except Exception:
            pass
        return False


def read_prompt(note):
    print("\n指示を入力(空行で確定)")
    if note:
        print("  ※ 前回の適用エラーを一緒に送ります。空行のみでエラーだけを再送")
    else:
        print(
            "  コマンド: /undo /redo /copy(pack を再コピー) /wait(コピー待ちを再開) /q  ・何も入力せず空行で終了"
        )
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
            break  # コマンドは空行を待たずに実行
    return "\n".join(lines)


def wait_reply(pack_id, seq0, state):
    """応答が揃うまで待って本文を返す。Ctrl+Q で中止したら None

    state["buf"] に途中までの応答を保持する(/wait で再開したとき引き継ぐ)。
    seq0 が None のときは、現在のクリップボードも応答の候補として扱う。
    """
    print("チャットの応答をコピーするのを待っています… (Ctrl+Q で中止、/wait で再開)")
    buf = state.get("buf")
    with _KeyPoller() as keys:
        while True:
            try:
                time.sleep(0.5)
                if keys.pressed():
                    break
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
                state["buf"] = buf
                if msg:
                    print(msg)
                if buf is None:
                    continue
                body = completed(buf, pack_id)
                if body is not None:
                    state["buf"] = None
                    return body
                if was_none:
                    print(
                        f"応答が途中で切れているようです(末尾の `### END {pack_id}` がありません)。"
                    )
                    print(
                        "チャットで「続き」と送り、続きをコピーしてください。(Ctrl+Q でここまでを反映)"
                    )
            except KeyboardInterrupt:
                # Ctrl+C はコピー操作と被るので、Ctrl+Q が使えるなら無視する
                if keys.available:
                    continue
                break
    return _abort(state, buf)


def _abort(state, buf):
    print()
    if buf is not None:
        ans = input("ここまでの内容で反映しますか? [y/N] ").strip().lower()
        if ans == "y":
            state["buf"] = None
            return buf
    print("中止しました。コピー待ちを再開するには /wait と入力してください。")
    return None


def _save_txt(pack, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(pack)


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
        return (
            "前回の応答は次の理由で適用できませんでした(ファイルは何も変更されていません)。"
            "SEARCH は現在のファイル内容から1文字も変えずにコピーしてやり直してください。\n"
            + "\n".join(f"- {e}" for e in plan.errors)
        )
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
    ap = argparse.ArgumentParser(
        prog="chat-coder", description="チャット経由コーディング用の半自動ループ"
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("root", nargs="?", default=".", help="プロジェクトのルート(省略時はカレント)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--window",
        metavar="TITLE_RE",
        help="チャット画面のウィンドウタイトル(正規表現)。指定すると pywinauto で自動入力する",
    )
    mode.add_argument(
        "--txt",
        nargs="?",
        const="",
        metavar="PATH",
        help="pack を txt ファイルに書き出す(省略時は .llm_backup/pack.txt)。"
        "自動入力はせず、このファイルをチャットに添付して送る(クリップボードにも入ります)",
    )
    ap.add_argument(
        "--type",
        action="store_true",
        help="--window と併用。貼り付けではなく1文字ずつ入力する(貼り付けで空白が崩れる場合)",
    )
    ap.add_argument(
        "--no-submit",
        action="store_true",
        help="--window と併用。入力だけして送信(Enter)はしない",
    )
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"フォルダが見つかりません: {root}")
    txt_path = Path(args.txt) if args.txt else root / BACKUP_DIRNAME / "pack.txt"
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    print(f"プロジェクト: {root}")
    if ensure_llmignore(root):
        print(
            f"除外ルール {LLMIGNORE} を既定の内容で作成しました。送りたくないファイルがあれば編集してください。"
        )
    elif not (root / LLMIGNORE).is_file():
        print(f"※ {LLMIGNORE} を作成できませんでした。内蔵の既定ルールで動作します。")
    history = History(root)
    note = ""
    last = None  # (pack, pack_id)
    state = {}  # 待機中の途中経過(/wait で再開するため保持)
    while True:
        instr = read_prompt(note)
        if instr is None:  # Ctrl+C: 入力だけ取り消す
            continue
        cmd = instr.strip().lower()
        if cmd == "/q" or (not instr and not note):
            break
        if cmd == "/wait":
            if last is None:
                print("まだ pack を作っていません。")
                continue
            reply = wait_reply(last[1], None, state)
            if reply is not None:
                beep()
                note = _handle_reply(root, history, reply)
            continue
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
            state = {}
        try:
            clipboard.set(pack)
        except OSError as e:
            print(f"クリップボードに書き込めません: {e}")
            continue
        seq0 = clipboard.seq()
        print(f"クリップボードにコピーしました({len(pack):,} 文字)。")
        if len(pack) > WARN_CHARS:
            print(
                "  ※ 文字数が多いです。チャットの入力上限に注意(超える場合はファイルを絞るか添付で渡す)。"
            )
        if args.txt is not None:
            try:
                _save_txt(pack, txt_path)
                print(f"pack を書き出しました: {txt_path}")
                print(
                    "チャットにこのファイルを添付して送信してください(クリップボードにも入っています)。"
                )
            except OSError as e:
                print(f"ファイルに書き出せません: {e}")
                print("チャットに貼り付けて送信してください。")
        else:
            sent = False
            if args.window:
                try:
                    autosend.send(pack, args.window, args.type, not args.no_submit)
                    sent = True
                    print("チャット画面に自動入力しました。")
                except Exception as e:
                    print(f"自動入力に失敗しました({type(e).__name__}: {e})。")
            if not sent:
                print("チャットに貼り付けて送信してください。")
        reply = wait_reply(pack_id, seq0, state)
        if reply is None:
            continue
        beep()
        note = _handle_reply(root, history, reply)


if __name__ == "__main__":
    main()
