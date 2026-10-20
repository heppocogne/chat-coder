"""クリップボードに入ってきたテキストが、今の送信に対する応答かを判定する(純粋関数のみ)"""

from __future__ import annotations

import re

from .patch import strip_blocks

HEADER_RE = re.compile(r"^### LLM_LOOP[ \t]+(\S+)[ \t]*$", re.M)
END_LINE_RE = re.compile(r"^### END(?:[ \t]+\S+)?[ \t]*$", re.M)  # 説明文の抽出用(ID は問わない)
PACK_RE = re.compile(r"^(?:# LLM_LOOP PACK|##### BEGIN FILE: )", re.M)
FENCE_RE = re.compile(r"^`{3,}\w*[ \t]*$")
BLOCK_RE = re.compile(r"^### (?:FILE|DELETE):", re.M)


def _strip_edge_fences(text):
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and FENCE_RE.match(lines[0]):
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and FENCE_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines) + "\n"


def header_id(buf):
    m = HEADER_RE.search(buf)
    return m.group(1) if m else None


def ingest(text, pack_id, buf, old_ids=()):
    """新しいクリップボード内容を取り込む。(更新後の buf, 利用者へのメッセージ)

    buf は応答の蓄積(None = まだ受信していない)。
    old_ids はこのセッションで過去に使った id。モデルは会話履歴の前の応答を真似て古い id を
    書きがちなので、今回の id の代わりに受け付ける(古い応答の取り違えは呼び出し側で防ぐ)。
    """
    t = text.replace("\r\n", "\n")
    if PACK_RE.search(t):  # 自分が送った pack をコピーし直しただけ
        return buf, ""
    m = HEADER_RE.search(t)
    if m:
        if m.group(1) in old_ids:
            return t[m.start() :], (
                f"応答の id が前回までの値({m.group(1)})でしたが、今回の応答として扱います。"
            )
        if m.group(1) != pack_id:
            return buf, f"別の送信({m.group(1)})への応答なので無視しました。"
        return t[m.start() :], ""
    if buf is None:
        if BLOCK_RE.search(t):
            return t, "※ 先頭の `### LLM_LOOP` 行が無い応答として扱います。"
        return None, ""  # 無関係なコピー
    # 途中で切れた応答の続き: 最後の行は書き直されるので捨ててから連結する
    s = buf.rstrip("\n")
    head = s[: s.rfind("\n") + 1]
    return head + _strip_edge_fences(t), "続きを連結しました。"


def completed(buf, pack_id, old_ids=()):
    """`### END <pack_id>` まで届いていれば、その手前までの本文。届いていなければ None

    ID が一致する行だけを終端とみなし、複数あれば最後の行を採る。
    コードや説明の中の単独行 `### END` では完了扱いにならない。
    old_ids の END 行(モデルが前回の id を写した場合)も終端として認める。
    """
    ids = {pack_id, *old_ids}
    pattern = "|".join(re.escape(i) for i in ids)
    last = None
    for m in re.finditer(rf"^### END[ \t]+(?:{pattern})[ \t]*$", buf, re.M):
        last = m
    return buf[: last.start()] if last else None


def extract_prose(reply):
    """変更ブロックと目印行を除いた、説明文だけを返す"""
    t = strip_blocks(reply)
    t = HEADER_RE.sub("", t)
    t = END_LINE_RE.sub("", t)
    t = _strip_edge_fences(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()
