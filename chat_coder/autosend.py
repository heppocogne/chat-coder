"""pywinauto でチャット画面へ pack を自動入力する(任意機能。pywinauto は初回利用時に import)"""

from __future__ import annotations

import time

_SPECIAL = set("{}+^%~()")


def to_keys(line):
    """1行分の文字列を pywinauto の send_keys 書式にする(特殊文字・空白・タブをエスケープ)"""
    out = []
    for ch in line:
        if ch in _SPECIAL:
            out.append("{" + ch + "}")
        elif ch == " ":
            out.append("{SPACE}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    return "".join(out)


def _focus(title_re):
    from pywinauto import Desktop

    win = Desktop(backend="uia").window(title_re=title_re, visible_only=True, found_index=0)
    win = win.wrapper_object()
    win.set_focus()
    time.sleep(0.4)
    return win


def send(pack, title_re, keystrokes=False, submit=True):
    """title_re に一致するウィンドウを前面に出して pack を入力する。

    入力欄にカーソルが置かれている状態を前提にする(ウィンドウを前面に出すだけで、欄は探さない)。
    keystrokes=False: クリップボードを Ctrl+V で貼る(速い)。
    keystrokes=True: 1文字ずつ打つ。改行は Shift+Enter(Enter では送信されてしまうため)。
      Tab はブラウザ側でフォーカス移動になるので、タブ文字を含むコードには向かない。
    """
    from pywinauto import keyboard

    _focus(title_re)
    if keystrokes:
        lines = pack.split("\n")
        for i, line in enumerate(lines):
            if line:
                keyboard.send_keys(to_keys(line), pause=0, with_spaces=True, with_tabs=True)
            if i < len(lines) - 1:
                keyboard.send_keys("+{ENTER}", pause=0)
    else:
        keyboard.send_keys("^v", pause=0)
        time.sleep(min(3.0, 0.5 + len(pack) / 100_000))
    if submit:
        keyboard.send_keys("{ENTER}", pause=0)
