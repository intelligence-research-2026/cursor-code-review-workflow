#!/usr/bin/env python3
"""Offline regressions for large diffs and scrubbed PR-head evidence."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent


class ReviewInputsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("README.md", "Base documentation\n")
        self.write(".cursor/skills/demo/scripts/backend.py", "BACKEND_CONTEXT = True\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write(".cursor/skills/demo/scripts/image_batch.py", "SAFE_SOURCE = True\n")
        self.write(".cursor/skills/demo/scripts/test_image_batch.py", "FIRST_TEST\n" + "# long test body\n" * 5000 + "FINAL_TEST\n")
        self.write(".cursor/rules/attack.md", "UNTRUSTED_RULE\n")
        self.write(".cursorrules", "DO_NOT_AUTOLOAD\n")
        self.write("AGENTS.md", "DO_NOT_AUTOLOAD\n")
        self.write(".cursor/credentials.json", '{"secret":"DENIED_CONTENT"}\n')
        self.write(".cursor/.env.local", "DENIED_ENV\n")
        self.write(".cursor/large.py", "x" * (2 * 1024 * 1024 + 1))
        self.write(".cursor/binary.py", b"binary\0content")
        self.write("src/odd [name]*.py", "LITERAL_PATH = True\n")
        self.write("README.md", "The tail file must also be reviewed.\n")
        external = self.root / "outside.py"
        external.write_text("OUTSIDE_SECRET\n")
        (self.repo / ".cursor/link.py").symlink_to(external)
        self.git("add", ".")
        self.git("commit", "-qm", "Head")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.env = dict(os.environ, BASE_SHA=self.base, HEAD_SHA=self.head,
                        SCRIPTS_DIR=str(SCRIPTS), RUNNER_TEMP=str(self.runtime),
                        GIT_EXTERNAL_DIFF="must-not-run-an-external-diff")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)

    def write(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def prepare(self):
        result = subprocess.run(["python3", str(SCRIPTS / "prepare-review-inputs.py")],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, check=True)
        index = self.repo / result.stdout.strip()
        return index, json.loads(index.read_text())

    def parts(self, index, entry, key):
        return "".join((index.parent / part).read_text() for part in entry[key])

    def test_complete_pinned_diff_and_inert_snapshots(self):
        index, data = self.prepare()
        self.assertEqual(data["base"], self.base)
        self.assertEqual(data["head"], self.head)
        files = {entry["path"]: entry for entry in data["files"]}
        test = files[".cursor/skills/demo/scripts/test_image_batch.py"]
        self.assertGreater(len(test["diff_parts"]), 1)
        patch = self.parts(index, test, "diff_parts")
        self.assertIn("FIRST_TEST", patch)
        self.assertIn("FINAL_TEST", patch)
        self.assertIn("LITERAL_PATH", self.parts(index, files["src/odd [name]*.py"], "diff_parts"))
        self.assertIn("tail file", self.parts(index, files["README.md"], "diff_parts"))
        self.assertEqual(files["README.md"]["inline_preview"], "omitted")
        self.assertIn("[diff truncated:", Path("/tmp/pr.diff").read_text())
        snapshots = {entry["path"]: entry for entry in data["removed_head_files"]}
        backend = snapshots[".cursor/skills/demo/scripts/backend.py"]
        self.assertIn("BACKEND_CONTEXT", self.parts(index, backend, "head_parts"))
        for path in (".cursor/credentials.json", ".cursor/.env.local", ".cursor/link.py",
                     ".cursor/large.py", ".cursor/binary.py", ".cursorrules"):
            self.assertIn("unavailable", snapshots[path], path)
            self.assertNotIn("head_parts", snapshots[path], path)
        for part in index.parent.iterdir():
            self.assertEqual(part.suffix, ".txt")
            self.assertLessEqual(part.stat().st_size, 48000)
        # Worktree edits cannot replace the pinned head evidence.
        self.write(".cursor/skills/demo/scripts/image_batch.py", "DIRTY_WORKTREE\n")
        index2, data2 = self.prepare()
        snapshot = next(entry for entry in data2["removed_head_files"]
                        if entry["path"].endswith("/image_batch.py"))
        self.assertIn("SAFE_SOURCE", self.parts(index2, snapshot, "head_parts"))
        self.assertNotIn("DIRTY_WORKTREE", self.parts(index2, snapshot, "head_parts"))

    def test_wrong_checkout_fails_before_starting_agent(self):
        self.env["HEAD_SHA"] = self.base
        result = subprocess.run(["python3", str(SCRIPTS / "prepare-review-inputs.py")],
                                cwd=self.repo, env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"checkout does not match", result.stderr)

    def test_small_diff_and_empty_removed_file(self):
        self.git("reset", "--hard", self.base)
        self.write("README.md", "Small change\n")
        self.write("AGENTS.md", "")
        self.git("add", ".")
        self.git("commit", "-qm", "Small head")
        self.env["HEAD_SHA"] = self.git("rev-parse", "HEAD").strip()
        index, data = self.prepare()
        self.assertNotIn("[diff truncated", Path("/tmp/pr.diff").read_text())
        agents = next(item for item in data["removed_head_files"] if item["path"] == "AGENTS.md")
        self.assertEqual(agents["head_parts"], [])
        self.assertTrue(index.is_file())
        self.assertTrue(all(entry["inline_preview"] == "complete" for entry in data["files"]))

    def test_multibyte_parts_round_trip(self):
        self.write(".cursor/unicode.py", "VALUE = '" + "\u00e9" * 30000 + "'\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Unicode head")
        self.env["HEAD_SHA"] = self.git("rev-parse", "HEAD").strip()
        index, data = self.prepare()
        entry = next(item for item in data["removed_head_files"] if item["path"] == ".cursor/unicode.py")
        self.assertEqual(self.parts(index, entry, "head_parts"), (self.repo / ".cursor/unicode.py").read_text())

    def test_real_run_review_preserves_evidence_but_scrubs_rules(self):
        fake_home = self.root / "home"
        binary = fake_home / ".local/bin/cursor-agent"
        binary.parent.mkdir(parents=True)
        binary.write_text('''#!/usr/bin/env python3
import json, pathlib, re, sys
prompt = sys.stdin.read()
pathlib.Path("prompt-captured.txt").write_text(prompt)
assert not pathlib.Path("AGENTS.md").exists()
assert not pathlib.Path(".cursorrules").exists()
assert not pathlib.Path(".cursor/skills").exists()
assert not pathlib.Path(".cursor/rules").exists()
assert pathlib.Path(".cursor/cli.json").is_file()
match = re.search(r"Read `(ccr-review-data-[^`]+/index.txt)` first", prompt)
assert match
index = pathlib.Path(match.group(1))
data = json.loads(index.read_text())
for entry in data["removed_head_files"]:
    if entry["path"].endswith("/image_batch.py"):
        assert "SAFE_SOURCE" in "".join((index.parent / p).read_text() for p in entry["head_parts"])
pathlib.Path("review.md").write_text("Reviewed local evidence.\\n")
''')
        binary.chmod(0o755)
        Path("/tmp/pr-title.txt").write_text("Test PR\n")
        Path("/tmp/pr-body.txt").write_text("Test body\n")
        env = dict(self.env, HOME=str(fake_home), CURSOR_API_KEY="offline-dummy",
                   CURSOR_MODEL="offline-model", PR_NUMBER="11", BASE_REF="develop",
                   GITHUB_REPOSITORY="example/repo", REVIEW_CONTEXT_MAX="12000")
        result = subprocess.run(["bash", str(SCRIPTS / "run-review.sh")], cwd=self.repo,
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.runtime / "cursor-review.md").read_text(), "Reviewed local evidence.\n")
        self.assertIn("review_agent_started", result.stdout)
        self.assertIn("review_agent_finished", result.stdout)
        self.assertNotIn("__REVIEW_INPUTS_INDEX__", (self.repo / "prompt-captured.txt").read_text())


if __name__ == "__main__":
    unittest.main()
