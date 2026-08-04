"""A directory pathspec must never introduce an untracked file into a commit.

4 Aug 2026: bot.py's _git_commit passes ["ClaudeCoach/"], so `git add -- ClaudeCoach/`
swept an untracked athletes.json BACKUP - every athlete's ICU api key and Telegram
chat_id - into commit 6909ec8 of a PUBLIC repo, under a message about logging a feel
note. .gitignore named ClaudeCoach/config/athletes.json by EXACT PATH, so a .backup-*
copy of it was never covered.

Directory pathspecs now stage with -u (tracked files only). Callers naming explicit
FILES keep a plain add, because publishing a genuinely new file is intended there.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CC / "lib"))


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


class DirectoryStagingIsTrackedOnly(unittest.TestCase):
    """Exercises the real git behaviour the fix relies on, in a throwaway repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _git(["init", "-q"], self.tmp)
        _git(["config", "user.email", "t@t"], self.tmp)
        _git(["config", "user.name", "t"], self.tmp)
        d = Path(self.tmp) / "sub"
        d.mkdir()
        (d / "tracked.json").write_text("{}")
        _git(["add", "-A"], self.tmp)
        _git(["commit", "-qm", "init"], self.tmp)
        (d / "tracked.json").write_text('{"changed": 1}')
        (d / "secret.backup-2026-08-04").write_text('{"icu_api_key": "SECRET"}')

    def _staged(self):
        return _git(["diff", "--cached", "--name-only"], self.tmp).stdout.split()

    def test_add_u_on_a_directory_stages_the_change_but_not_the_untracked_file(self):
        _git(["add", "-u", "--", "sub/"], self.tmp)
        staged = self._staged()
        self.assertIn("sub/tracked.json", staged)
        self.assertNotIn("sub/secret.backup-2026-08-04", staged)

    def test_plain_add_on_a_directory_is_what_leaked(self):
        # The behaviour being fixed — pinned so nobody "simplifies" it back.
        _git(["add", "--", "sub/"], self.tmp)
        self.assertIn("sub/secret.backup-2026-08-04", self._staged())

    def test_explicit_file_pathspec_still_adds_a_new_file(self):
        # Callers naming files rely on this (a first-ever public/*.json must publish).
        _git(["add", "--", "sub/secret.backup-2026-08-04"], self.tmp)
        self.assertIn("sub/secret.backup-2026-08-04", self._staged())


class HelperChoosesTheRightMode(unittest.TestCase):
    def test_sync_commit_push_uses_dash_u_for_a_directory(self):
        src = (CC / "lib" / "git_sync.py").read_text()
        self.assertIn('["git", "add", "-u", "--", p] if is_dir else', src)

    def test_a_trailing_slash_counts_as_a_directory_without_touching_disk(self):
        # bot.py passes "ClaudeCoach/" — it must be treated as a directory even if the
        # path check cannot resolve it from the current working directory.
        src = (CC / "lib" / "git_sync.py").read_text()
        self.assertIn('p.endswith("/")', src)


if __name__ == "__main__":
    unittest.main()
