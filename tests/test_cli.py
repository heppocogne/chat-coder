import tempfile
import unittest
from pathlib import Path

from chat_coder.cli import _save_txt


class SaveTxtTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()

    def test_creates_parent_and_keeps_text_as_is(self):
        pack = "# LLM_LOOP PACK\r\nあいう\n"
        path = self.root / ".llm_backup" / "pack.txt"
        _save_txt(pack, path)
        self.assertEqual(path.read_bytes(), pack.encode("utf-8"))

    def test_overwrites_previous_pack(self):
        path = self.root / "pack.txt"
        _save_txt("first", path)
        _save_txt("second", path)
        self.assertEqual(path.read_text(encoding="utf-8"), "second")


if __name__ == "__main__":
    unittest.main()
