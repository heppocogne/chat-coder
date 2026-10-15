import tempfile
import unittest
from pathlib import Path

from chat_coder.patch import History, HistoryError, plan_changes
from chat_coder.project import PACK_MARK, build_pack, collect_files, decode_bytes
from chat_coder.reply import completed, extract_prose, ingest


def edit(rel, old, new):
    return f"### FILE: {rel}\n<<<<<<< SEARCH\n{old}=======\n{new}>>>>>>> REPLACE\n"


class TempRoot(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()

    def write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p


class PatchTest(TempRoot):
    def test_edit_new_delete(self):
        self.write("a.py", "x = 1\ny = 2\n")
        self.write("old.txt", "bye\n")
        reply = (edit("a.py", "x = 1\n", "x = 10\n")
                 + edit("sub/new.py", "", "print('hi')\n")
                 + "### DELETE: old.txt\n")
        plan = plan_changes(self.root, reply)
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.n_ops, 3)
        History(self.root).commit(plan.changes)
        self.assertEqual((self.root / "a.py").read_text(), "x = 10\ny = 2\n")
        self.assertEqual((self.root / "sub/new.py").read_text(), "print('hi')\n")
        self.assertFalse((self.root / "old.txt").exists())

    def test_crlf_bom_and_cp932_preserved(self):
        self.write("crlf.txt", b"\xef\xbb\xbfa\r\nb\r\n")
        self.write("sjis.txt", "こんにちは\nworld\n".encode("cp932"))
        reply = edit("crlf.txt", "b\n", "c\n") + edit("sjis.txt", "world\n", "世界\n")
        plan = plan_changes(self.root, reply)
        self.assertEqual(plan.errors, [])
        History(self.root).commit(plan.changes)
        self.assertEqual((self.root / "crlf.txt").read_bytes(), b"\xef\xbb\xbfa\r\nc\r\n")
        self.assertEqual((self.root / "sjis.txt").read_bytes(), "こんにちは\n世界\n".encode("cp932"))

    def test_unencodable_char_is_error(self):
        self.write("sjis.txt", "あ\n".encode("cp932"))
        plan = plan_changes(self.root, edit("sjis.txt", "あ\n", "😀\n"))
        self.assertTrue(plan.errors)

    def test_failure_changes_nothing(self):
        p = self.write("a.py", "x = 1\n")
        reply = edit("a.py", "x = 1\n", "x = 2\n") + edit("a.py", "zzz\n", "q\n")
        plan = plan_changes(self.root, reply)
        self.assertTrue(plan.errors)
        self.assertEqual(p.read_text(), "x = 1\n")
        self.assertFalse((self.root / ".llm_backup").exists())

    def test_truncated_block_is_error(self):
        self.write("a.py", "x = 1\n")
        reply = edit("a.py", "x = 1\n", "x = 2\n") + "### FILE: a.py\n<<<<<<< SEARCH\nx = 2\n=====\n"
        plan = plan_changes(self.root, reply)
        self.assertTrue(plan.errors)

    def test_path_escape_rejected(self):
        plan = plan_changes(self.root, edit("../evil.py", "", "x\n"))
        self.assertTrue(plan.errors)
        for rel in (".git/config", "./.git/config", "sub/../.git/config", ".GIT/config",
                    ".git /config", "a/.git/hooks/x", ".llm_backup/x", "C:/Windows/x", ".",
                    "//host/share/x", r"\\host\share\x", "/abs/x", "C:x"):
            plan = plan_changes(self.root, edit(rel, "", "x\n"))
            self.assertTrue(plan.errors, rel)

    def test_dotslash_path_is_canonicalized(self):
        plan = plan_changes(self.root, edit("./sub/../new.py", "", "x\n"))
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.changes[0].rel, "new.py")

    def test_no_change_when_identical(self):
        self.write("a.py", "x = 1\n")
        plan = plan_changes(self.root, edit("a.py", "x = 1\n", "x = 1\n"))
        self.assertEqual((plan.errors, plan.changes), ([], []))
        self.assertEqual(plan.n_ops, 1)

    def test_answer_only(self):
        plan = plan_changes(self.root, "ただの説明です。\n")
        self.assertEqual((plan.errors, plan.changes, plan.n_ops), ([], [], 0))


class HistoryTest(TempRoot):
    def test_undo_redo_and_conflict(self):
        a = self.write("a.py", "x = 1\n")
        h = History(self.root)
        plan = plan_changes(self.root, edit("a.py", "x = 1\n", "x = 2\n") + edit("n.py", "", "new\n"))
        h.commit(plan.changes)
        self.assertEqual((self.root / ".llm_backup" / ".gitignore").read_text(), "*\n")

        h.undo()
        self.assertEqual(a.read_text(), "x = 1\n")
        self.assertFalse((self.root / "n.py").exists())
        h.redo()
        self.assertEqual(a.read_text(), "x = 2\n")
        self.assertEqual((self.root / "n.py").read_text(), "new\n")

        a.write_text("手で編集\n", encoding="utf-8")
        with self.assertRaises(HistoryError) as cm:
            h.undo()
        self.assertEqual(cm.exception.conflicts, ["a.py"])
        h.undo(force=True)
        self.assertEqual(a.read_text(), "x = 1\n")

    def test_empty_history(self):
        with self.assertRaises(HistoryError):
            History(self.root).undo()

    def test_new_commit_clears_redo(self):
        self.write("a.py", "1\n")
        h = History(self.root)
        h.commit(plan_changes(self.root, edit("a.py", "1\n", "2\n")).changes)
        h.undo()
        h.commit(plan_changes(self.root, edit("a.py", "1\n", "3\n")).changes)
        with self.assertRaises(HistoryError):
            h.redo()


class ProjectTest(TempRoot):
    def test_collect_reports_skipped_with_reason(self):
        self.write("ok.py", "print(1)\n")
        self.write("img.bin", b"\x00\x01\x02")
        self.write("big.txt", b"a" * 200_001)
        self.write("sjis.txt", "日本語\n".encode("cp932"))
        self.write("weird.dat", b"\xff\xfe\xfd\xfc\x81")
        files, skipped = collect_files(self.root)
        self.assertEqual(sorted(r for r, _ in files), ["ok.py", "sjis.txt"])
        self.assertEqual(dict(skipped).keys(), {"img.bin", "big.txt", "weird.dat"})

    def test_secret_files_not_packed(self):
        self.write("ok.py", "x\n")
        self.write(".env", "TOKEN=1\n")
        self.write(".env.local", "TOKEN=1\n")
        self.write("deploy.pem", "KEY\n")
        files, skipped = collect_files(self.root)
        self.assertEqual([r for r, _ in files], ["ok.py"])
        self.assertEqual({r for r, _ in skipped}, {".env", ".env.local", "deploy.pem"})

    def test_decode(self):
        self.assertEqual(decode_bytes(b"a\r\nb"), ("a\nb", "utf-8"))
        self.assertEqual(decode_bytes(b"\xef\xbb\xbfa")[1], "utf-8-sig")

    def test_pack_has_marker_and_id(self):
        pack = build_pack([("a.py", "x\n")], "do it", "", "abc123")
        self.assertTrue(pack.startswith(PACK_MARK))
        self.assertIn("### LLM_LOOP abc123", pack)
        self.assertIn("##### BEGIN FILE: a.py", pack)


class ReplyTest(unittest.TestCase):
    ID = "abc123"

    def test_own_pack_ignored(self):
        pack = build_pack([], "x", "", self.ID)
        self.assertEqual(ingest(pack, self.ID, None), (None, ""))

    def test_pack_with_files_ignored_even_without_mark_line(self):
        t = "##### BEGIN FILE: a.py\nx\n##### END FILE\n### FILE: a.py\n"
        self.assertEqual(ingest(t, self.ID, None), (None, ""))

    def test_stale_id_skipped(self):
        buf, msg = ingest("### LLM_LOOP zzz999\n### END\n", self.ID, None)
        self.assertIsNone(buf)
        self.assertIn("zzz999", msg)

    def test_full_copy_with_fences(self):
        t = f"````\n### LLM_LOOP {self.ID}\n説明\n{edit('a.py', 'a\n', 'b\n')}### END\n````\n"
        buf, _ = ingest(t, self.ID, None)
        body = completed(buf)
        self.assertIsNotNone(body)
        self.assertNotIn("### END", body)
        self.assertEqual(extract_prose(body), "説明")

    def test_answer_only_is_complete(self):
        buf, _ = ingest(f"### LLM_LOOP {self.ID}\nはい、そうです。\n### END", self.ID, None)
        self.assertEqual(completed(buf), f"### LLM_LOOP {self.ID}\nはい、そうです。\n")

    def test_truncated_then_continued(self):
        first = (f"### LLM_LOOP {self.ID}\n### FILE: a.py\n<<<<<<< SEARCH\nx = 1\n=======\n"
                 "x = 2\ny = ")
        buf, _ = ingest(first, self.ID, None)
        self.assertIsNone(completed(buf))
        cont = "````\ny = 3\n>>>>>>> REPLACE\n### END\n````"
        buf, msg = ingest(cont, self.ID, buf)
        self.assertEqual(msg, "続きを連結しました。")
        body = completed(buf)
        self.assertIsNotNone(body)
        self.assertIn("x = 2\ny = 3\n>>>>>>> REPLACE\n", body)

    def test_unrelated_clipboard_ignored(self):
        self.assertEqual(ingest("hello", self.ID, None), (None, ""))

    def test_missing_header_but_blocks_accepted_with_warning(self):
        buf, msg = ingest(edit("a.py", "a\n", "b\n"), self.ID, None)
        self.assertIsNotNone(buf)
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main()
