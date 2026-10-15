import tempfile
import unittest
from pathlib import Path

from chat_coder.ignore import IgnoreMatcher, ensure_llmignore


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def matcher(self, gitignore="", **files):
        if gitignore:
            self.write(".gitignore", gitignore)
        for rel, text in files.items():
            self.write(rel.replace("__", "/"), text)
        return IgnoreMatcher(self.root)

    def test_defaults(self):
        m = self.matcher()
        for rel in (
            "node_modules/x.js",
            "a/node_modules/x.js",
            ".git/config",
            ".llm_backup/a/b",
            "src/__pycache__/m.pyc",
            "yarn.lock",
            "sub/package-lock.json",
            ".vscode/s.json",
        ):
            self.assertTrue(m.is_ignored(rel), rel)
        for rel in ("src/main.py", ".env", "build.py", "README.md"):
            self.assertFalse(m.is_ignored(rel), rel)

    def test_basename_glob_any_depth(self):
        m = self.matcher("*.log\n")
        self.assertTrue(m.is_ignored("a.log"))
        self.assertTrue(m.is_ignored("sub/deep/b.log"))
        self.assertFalse(m.is_ignored("a.logx"))

    def test_anchored(self):
        m = self.matcher("/out\ndocs/*.md\n")
        self.assertTrue(m.is_ignored("out"))
        self.assertFalse(m.is_ignored("sub/out"))
        self.assertTrue(m.is_ignored("docs/a.md"))
        self.assertFalse(m.is_ignored("docs/x/a.md"))
        self.assertFalse(m.is_ignored("other/docs/a.md"))

    def test_double_star(self):
        m = self.matcher("**/cache\na/**/z\nlogs/**\n")
        self.assertTrue(m.is_ignored("cache"))
        self.assertTrue(m.is_ignored("x/y/cache"))
        self.assertTrue(m.is_ignored("a/z"))
        self.assertTrue(m.is_ignored("a/b/c/z"))
        self.assertTrue(m.is_ignored("logs/x/y.txt"))
        self.assertFalse(m.is_ignored("b/z"))

    def test_dir_only(self):
        m = self.matcher("tmp/\n")
        self.assertTrue(m.is_ignored("tmp/a.txt"))
        self.assertTrue(m.is_ignored("sub/tmp/a.txt"))
        self.assertFalse(m.is_ignored("tmp"))  # 同名のファイルは対象外
        self.assertTrue(m.is_ignored("tmp", is_dir=True))

    def test_negation_and_parent_exclusion(self):
        m = self.matcher("*.log\n!keep.log\nbuild2/\n!build2/x.txt\n")
        self.assertTrue(m.is_ignored("a.log"))
        self.assertFalse(m.is_ignored("keep.log"))
        self.assertTrue(m.is_ignored("build2/x.txt"))  # 親が除外されていれば再包含できない

    def test_character_class_question_and_escape(self):
        m = self.matcher("f[0-9].txt\nq?.py\n\\#hash.txt\n")
        self.assertTrue(m.is_ignored("f3.txt"))
        self.assertFalse(m.is_ignored("fx.txt"))
        self.assertTrue(m.is_ignored("qa.py"))
        self.assertFalse(m.is_ignored("q.py"))
        self.assertTrue(m.is_ignored("#hash.txt"))

    def test_comments_and_blank_lines(self):
        m = self.matcher("# comment\n\n   \n*.tmp   \n")
        self.assertTrue(m.is_ignored("a.tmp"))
        self.assertFalse(m.is_ignored("# comment"))

    def test_nested_gitignore_is_relative_to_its_dir(self):
        m = self.matcher(**{"sub__.gitignore": "*.tmp\n/only\n"})
        self.assertTrue(m.is_ignored("sub/a.tmp"))
        self.assertTrue(m.is_ignored("sub/d/a.tmp"))
        self.assertTrue(m.is_ignored("sub/only"))
        self.assertFalse(m.is_ignored("a.tmp"))
        self.assertFalse(m.is_ignored("sub/d/only"))
        self.assertFalse(m.is_ignored("only"))

    def test_llmignore_is_evaluated_after_gitignore(self):
        m = self.matcher("*.sql\n", **{".llmignore": "!data.sql\nsecret/\n"})
        self.assertTrue(m.is_ignored("a.sql"))
        self.assertFalse(m.is_ignored("data.sql"))  # .llmignore の否定が勝つ
        self.assertTrue(m.is_ignored("secret/k.txt"))

    def test_llmignore_replaces_builtin_defaults(self):
        m = self.matcher(**{".llmignore": "*.tmp\n"})
        self.assertTrue(m.is_ignored("a.tmp"))
        self.assertFalse(m.is_ignored("node_modules/x.js"))  # 既定の行を消せば送られる
        self.assertFalse(m.is_ignored("yarn.lock"))

    def test_git_and_backup_dirs_always_ignored(self):
        m = self.matcher("!.git/\n!.llm_backup/\n", **{".llmignore": "!.git/\n"})
        self.assertTrue(m.is_ignored(".git/config"))
        self.assertTrue(m.is_ignored("sub/.git/config"))
        self.assertTrue(m.is_ignored(".llm_backup/20260101/before/a.py"))
        self.assertTrue(m.is_ignored(".GIT/config"))

    def test_ensure_llmignore_creates_defaults_once(self):
        self.assertTrue(ensure_llmignore(self.root))
        path = self.root / ".llmignore"
        text = path.read_text(encoding="utf-8")
        self.assertIn("node_modules/", text)
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored("node_modules/x.js"))
        self.assertTrue(m.is_ignored("yarn.lock"))
        self.assertTrue(m.is_ignored(".env"))  # 秘密情報の既定行
        self.assertTrue(m.is_ignored("id_rsa"))
        self.assertFalse(m.is_ignored(".env.local"))  # .env.* は既定ではコメントアウト
        path.write_text("# 空にした\n", encoding="utf-8")
        self.assertFalse(ensure_llmignore(self.root))  # 既存は上書きしない
        self.assertEqual(path.read_text(encoding="utf-8"), "# 空にした\n")

    def test_uncommenting_secret_hint_works(self):
        ensure_llmignore(self.root)
        p = self.root / ".llmignore"
        p.write_text(p.read_text(encoding="utf-8").replace("# .env\n", ".env\n"), encoding="utf-8")
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored(".env"))
        self.assertFalse(m.is_ignored(".env.local"))

    def test_unreadable_llmignore_falls_back_to_defaults(self):
        (self.root / ".llmignore").mkdir()  # 読めない形(ディレクトリ)にしておく
        m = IgnoreMatcher(self.root)
        self.assertTrue(m.is_ignored("node_modules/x.js"))
        self.assertTrue(m.is_ignored(".git/config"))

    def test_overlong_pattern_is_skipped(self):
        m = self.matcher("*" * 300 + "\n*.ok\n")
        self.assertFalse(m.is_ignored("a" * 300))
        self.assertTrue(m.is_ignored("a.ok"))

    def test_invalid_pattern_is_skipped(self):
        m = self.matcher("[\n*.ok\n")
        self.assertTrue(m.is_ignored("a.ok"))


if __name__ == "__main__":
    unittest.main()
