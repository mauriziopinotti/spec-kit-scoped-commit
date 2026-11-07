"""Local Git integration tests: no network, no real user worktree affected."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scoped_commit.py"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class ScopedCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Scoped Commit Tests")
        git(self.repo, "config", "user.email", "scoped-commit@example.invalid")
        (self.repo / ".specify").mkdir()
        (self.repo / ".gitignore").write_text(".specify/feature.json\n")
        (self.repo / "alpha.txt").write_text("before alpha\n")
        (self.repo / "beta.txt").write_text("before beta\n")
        (self.repo / "shared.txt").write_text("".join(f"line{i:02d}\n" for i in range(1, 31)))
        git(self.repo, "add", ".gitignore", "alpha.txt", "beta.txt", "shared.txt")
        git(self.repo, "commit", "-qm", "Initial")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def script(self, *args: str, success: bool = True):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("SPECIFY_FEATURE") or key == "GIT_INDEX_FILE":
                env.pop(key)
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo,
                              capture_output=True, text=True, env=env)
        if success and proc.returncode:
            self.fail(f"CLI failed ({proc.returncode}): {proc.stderr}\n{proc.stdout}")
        return proc

    def manifest(self, files, close=None):
        path = self.repo / ".git" / "scoped-manifest.json"
        path.write_text(json.dumps({"message": "test(scoped): selected changes", "files": files,
                                    "close_feature": close}))
        return path

    def preview(self, path: Path) -> str:
        output = self.script("preview", "--manifest", str(path)).stdout
        return re.search(r"APPROVAL TOKEN:\s+([a-f0-9]{20})", output).group(1)

    def commit(self, path: Path) -> str:
        token = self.preview(path)
        return self.script("commit", "--manifest", str(path), "--confirm", token).stdout

    def test_unrelated_staged_and_unstaged_preserved(self):
        (self.repo / "alpha.txt").write_text("chat change\n")
        (self.repo / "beta.txt").write_text("other staged\n")
        git(self.repo, "add", "beta.txt")
        (self.repo / "other.txt").write_text("untracked other\n")
        original_other = (self.repo / "other.txt").read_bytes()
        output = self.commit(self.manifest([{"path": "alpha.txt", "mode": "whole"}]))
        self.assertIn("No push was performed", output)
        self.assertEqual(git(self.repo, "show", "HEAD:alpha.txt"), "chat change")
        self.assertEqual(git(self.repo, "show", "HEAD:beta.txt"), "before beta")
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "beta.txt")
        self.assertEqual((self.repo / "other.txt").read_bytes(), original_other)
        self.assertEqual(git(self.repo, "status", "--short").splitlines(), ["M  beta.txt", "?? other.txt"])

    def test_partial_hunk_leaves_other_hunk_in_worktree(self):
        original = (self.repo / "shared.txt").read_text().splitlines(keepends=True)
        original[1] = "CHAT-change\n"
        original[24] = "UNRELATED-change\n"
        (self.repo / "shared.txt").write_text("".join(original))
        inspect = self.script("inspect").stdout
        self.assertIn("[1] @@", inspect)
        self.assertIn("[2] @@", inspect)
        self.commit(self.manifest([{"path": "shared.txt", "mode": "hunks", "hunks": [1]}]))
        head_file = git(self.repo, "show", "HEAD:shared.txt")
        self.assertIn("CHAT-change", head_file)
        self.assertIn("line25", head_file)
        self.assertNotIn("UNRELATED-change", head_file)
        self.assertIn("UNRELATED-change", (self.repo / "shared.txt").read_text())
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.repo, "diff", "--name-only"), "shared.txt")

    def test_partial_staged_selected_path_refused(self):
        (self.repo / "shared.txt").write_text("CHAT\n" + (self.repo / "shared.txt").read_text())
        git(self.repo, "add", "shared.txt")
        (self.repo / "shared.txt").write_text((self.repo / "shared.txt").read_text() + "UNRELATED\n")
        path = self.manifest([{"path": "shared.txt", "mode": "hunks", "hunks": [1]}])
        err = self.script("preview", "--manifest", str(path), success=False)
        self.assertNotEqual(err.returncode, 0)
        self.assertIn("pre-existing staged", err.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_token_stales_when_selected_change_changes(self):
        (self.repo / "alpha.txt").write_text("chat1\n")
        path = self.manifest([{"path": "alpha.txt", "mode": "whole"}])
        approval = self.preview(path)
        (self.repo / "alpha.txt").write_text("chat2\n")
        err = self.script("commit", "--manifest", str(path), "--confirm", approval, success=False)
        self.assertNotEqual(err.returncode, 0)
        self.assertIn("Approval token", err.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_close_complete_feature_only_after_commit(self):
        folder = self.repo / "specs" / "007-login"
        folder.mkdir(parents=True)
        (folder / "spec.md").write_text("# Login spec\n")
        (folder / "tasks.md").write_text("- [x] Task A\n- [X] Task B\n")
        (self.repo / ".specify" / "feature.json").write_text(json.dumps({"feature_directory": "specs/007-login"}))
        (self.repo / "alpha.txt").write_text("chat change\n")
        path = self.manifest([{"path": "alpha.txt", "mode": "whole"},
                              {"path": "specs/007-login/spec.md", "mode": "whole"},
                              {"path": "specs/007-login/tasks.md", "mode": "whole"}], close="specs/007-login")
        self.commit(path)
        self.assertFalse((self.repo / ".specify" / "feature.json").exists())
        self.assertTrue((folder / "spec.md").exists())
        self.assertTrue((folder / "tasks.md").exists())
        self.assertEqual(git(self.repo, "show", "HEAD:specs/007-login/spec.md"), "# Login spec")

    def test_do_not_close_with_unchecked_task(self):
        folder = self.repo / "specs" / "008-pending"
        folder.mkdir(parents=True)
        (folder / "tasks.md").write_text("- [ ] unfinished\n")
        (self.repo / ".specify" / "feature.json").write_text(json.dumps({"feature_directory": "specs/008-pending"}))
        (self.repo / "alpha.txt").write_text("chat change\n")
        err = self.script("preview", "--manifest", str(self.manifest([
            {"path": "alpha.txt", "mode": "whole"},
            {"path": "specs/008-pending/tasks.md", "mode": "whole"}], close="specs/008-pending")), success=False)
        self.assertNotEqual(err.returncode, 0)
        self.assertIn("Feature not complete", err.stderr)
        self.assertTrue((self.repo / ".specify" / "feature.json").exists())

    def test_close_refuses_unspecified_feature_modification(self):
        folder = self.repo / "specs" / "009-profile"
        folder.mkdir(parents=True)
        (folder / "tasks.md").write_text("- [x] done\n")
        (folder / "spec.md").write_text("unrelated changed spec\n")
        (self.repo / ".specify" / "feature.json").write_text(json.dumps({"feature_directory": "specs/009-profile"}))
        (self.repo / "alpha.txt").write_text("chat change\n")
        err = self.script("preview", "--manifest", str(self.manifest([
            {"path": "alpha.txt", "mode": "whole"}], close="specs/009-profile")), success=False)
        self.assertNotEqual(err.returncode, 0)
        self.assertIn("remain unselected", err.stderr)

    def test_no_change_refused(self):
        err = self.script("preview", "--manifest", str(self.manifest([
            {"path": "alpha.txt", "mode": "whole"}])), success=False)
        self.assertNotEqual(err.returncode, 0)
        self.assertIn("no current changes", err.stderr)

    def test_select_second_hunk_only(self):
        original = (self.repo / "shared.txt").read_text().splitlines(keepends=True)
        original[1] = "UNRELATED-early\n"
        original[24] = "CHAT-late\n"
        (self.repo / "shared.txt").write_text("".join(original))
        manifest = self.manifest([{"path": "shared.txt", "mode": "hunks", "hunks": [2]}])
        preview = self.script("preview", "--manifest", str(manifest)).stdout
        self.assertIn("Excluded hunks in shared.txt", preview)
        self.commit(manifest)
        tree_file = git(self.repo, "show", "HEAD:shared.txt")
        self.assertIn("CHAT-late", tree_file)
        self.assertNotIn("UNRELATED-early", tree_file)
        self.assertIn("UNRELATED-early", (self.repo / "shared.txt").read_text())

    def test_selected_new_and_deleted_file(self):
        (self.repo / "alpha.txt").unlink()
        (self.repo / "fresh.txt").write_text("new chat file\n")
        self.commit(self.manifest([{"path": "alpha.txt", "mode": "whole"},
                                   {"path": "fresh.txt", "mode": "whole"}]))
        self.assertNotIn("alpha.txt", git(self.repo, "ls-tree", "--name-only", "HEAD"))
        self.assertEqual(git(self.repo, "show", "HEAD:fresh.txt"), "new chat file")
        self.assertEqual(git(self.repo, "status", "--short"), "")

    def test_approval_not_valid_for_changed_commit_message(self):
        (self.repo / "alpha.txt").write_text("chat1\n")
        path = self.manifest([{"path": "alpha.txt", "mode": "whole"}])
        token = self.preview(path)
        obj = json.loads(path.read_text())
        obj["message"] = "A different message"
        path.write_text(json.dumps(obj))
        result = self.script("commit", "--manifest", str(path), "--confirm", token, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_unrelated_active_feature_not_closed(self):
        folder = self.repo / "specs" / "010-another"
        folder.mkdir(parents=True)
        (folder / "tasks.md").write_text("- [ ] still ongoing\n")
        (self.repo / ".specify" / "feature.json").write_text(json.dumps({"feature_directory": "specs/010-another"}))
        (self.repo / "alpha.txt").write_text("a quick unrelated fix\n")
        self.commit(self.manifest([{"path": "alpha.txt", "mode": "whole"}]))
        self.assertTrue((self.repo / ".specify" / "feature.json").exists())


if __name__ == "__main__":
    unittest.main()
