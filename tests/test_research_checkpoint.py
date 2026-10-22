"""Isolated Git transaction checks; the real workspace is never committed."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.research_checkpoint import CheckpointError, checkpoint


REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO / "tmp/tests"


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="checkpoint-", dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.git("init", "-q")
        self.git("config", "user.name", "Research checkpoint test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        self.notebook = self.root / "notebook"
        (self.notebook / "records").mkdir(parents=True)
        (self.notebook / "snapshots").mkdir()
        self.write("unrelated.txt", "original\n")
        self.write("notebook/CURRENT.md", "initial\n")
        self.write("notebook/ledger.jsonl", '{"id":"old","path":"records/old.md"}\n')
        self.write("notebook/records/old.md", "historical\n")
        self.write("notebook/snapshots/old.bin", b"raw\x00\r\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.names = ["notebook/CURRENT.md", "notebook/ledger.jsonl",
                      "notebook/records/old.md", "notebook/snapshots/old.bin",
                      "notebook/checkpoint.json"]
        self.configure()

    def write(self, name, value):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode() if isinstance(value, str) else value)

    def configure(self):
        self.write("notebook/checkpoint.json", json.dumps({"schema_version": 1,
                                                          "managed_paths": self.names}))

    def git(self, *args, ok=0):
        result = subprocess.run(["git", "-C", str(self.root), *args],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, ok, result.stderr.decode(errors="replace"))
        return result.stdout

    def index_entries(self):
        return {row.split(b"\t", 1)[1]: row.split(b"\t", 1)[0]
                for row in self.git("ls-files", "--stage", "-z").split(b"\0") if row}

    def test_commit_preserves_unrelated_staged_unstaged_and_untracked(self):
        self.write("unrelated.txt", "staged\n")
        self.git("add", "unrelated.txt")
        self.write("unrelated.txt", "unstaged as well\n")
        self.write("outside-new.txt", "untracked\n")
        self.write("notebook/CURRENT.md", "updated\n")
        self.write("notebook/new note.md", "new managed\n")
        self.names.append("notebook/new note.md")
        self.configure()
        before = self.index_entries()[b"unrelated.txt"]
        result = checkpoint(self.root, self.notebook, "Research checkpoint")
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.index_entries()[b"unrelated.txt"], before)
        self.assertEqual(self.git("show", "HEAD:unrelated.txt"), b"original\n")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "unstaged as well\n")
        changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").decode().splitlines()
        self.assertEqual(set(changed), set(result["files"]))
        self.assertNotIn("outside-new.txt", changed)
        self.assertFalse(result["pushed"])

    def test_fully_staged_managed_change_is_supported(self):
        self.write("notebook/CURRENT.md", "staged managed\n")
        self.git("add", "notebook/CURRENT.md")
        result = checkpoint(self.root, self.notebook, "Managed staged change")
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")

    def test_partial_staging_refused_without_touching_index_or_head(self):
        self.write("notebook/CURRENT.md", "first edit\n")
        self.git("add", "notebook/CURRENT.md")
        self.write("notebook/CURRENT.md", "second edit\n")
        index = (self.root / ".git/index").read_bytes()
        head = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(CheckpointError, "Partially staged"):
            checkpoint(self.root, self.notebook, "Refuse partial")
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse((self.root / ".git/index.lock").exists())

    def test_second_checkpoint_is_noop(self):
        checkpoint(self.root, self.notebook, "First")
        before = (self.root / ".git/index").read_bytes()
        result = checkpoint(self.root, self.notebook, "No changes")
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual((self.root / ".git/index").read_bytes(), before)

    def test_ledger_rewrite_and_immutable_edit_or_delete_refused(self):
        for name, payload, error in [
            ("notebook/ledger.jsonl", '{"id":"replacement","path":"records/old.md"}\n', "append-only"),
            ("notebook/records/old.md", "altered\n", "immutable"),
            ("notebook/snapshots/old.bin", b"modified", "immutable"),
        ]:
            with self.subTest(name=name):
                old = (self.root / name).read_bytes()
                self.write(name, payload)
                with self.assertRaisesRegex(CheckpointError, error):
                    checkpoint(self.root, self.notebook, "Damaged history")
                self.write(name, old)
        (self.notebook / "records/old.md").unlink()
        with self.assertRaisesRegex(CheckpointError, "missing"):
            checkpoint(self.root, self.notebook, "Missing history")

    def test_append_only_ledger_change_is_allowed(self):
        self.write("notebook/records/new.md", "new record\n")
        with (self.notebook / "ledger.jsonl").open("ab") as target:
            target.write(b'{"id":"new","path":"records/new.md"}\n')
        result = checkpoint(self.root, self.notebook, "Append history")
        self.assertEqual(result["status"], "committed")
        self.assertIn("notebook/records/new.md", result["files"])

    def test_registered_snapshot_is_included_without_directory_sweep(self):
        self.write("notebook/snapshots/new.bin", b"new bytes")
        self.write("notebook/snapshots/unregistered.bin", b"do not commit")
        self.write("notebook/inventory.json", json.dumps({"artifacts": [
            {"snapshot_path": "snapshots/new.bin"}]}))
        self.names.append("notebook/inventory.json")
        self.configure()
        result = checkpoint(self.root, self.notebook, "Registered snapshot")
        self.assertIn("notebook/snapshots/new.bin", result["files"])
        self.assertNotIn("notebook/snapshots/unregistered.bin", result["files"])

    def test_unmanaged_historical_file_is_still_protected(self):
        self.names.remove("notebook/records/old.md")
        self.configure()
        self.write("notebook/records/old.md", "altered without allowlist\n")
        with self.assertRaisesRegex(CheckpointError, "immutable"):
            checkpoint(self.root, self.notebook, "Do not bypass protection")

    def test_missing_identity_and_commit_failure_leave_index_and_head(self):
        from scripts import research_checkpoint as module
        real_git = module._git
        for command in ("var", "commit-tree", "update-ref"):
            with self.subTest(command=command):
                old_index = (self.root / ".git/index").read_bytes()
                old_head = self.git("rev-parse", "HEAD")

                def fail(root, *args, **kwargs):
                    if args[0] == command:
                        raise CheckpointError("simulated missing identity or commit failure")
                    return real_git(root, *args, **kwargs)

                with patch.object(module, "_git", side_effect=fail):
                    with self.assertRaises(CheckpointError):
                        checkpoint(self.root, self.notebook, "Failure")
                self.assertEqual((self.root / ".git/index").read_bytes(), old_index)
                self.assertEqual(self.git("rev-parse", "HEAD"), old_head)
                self.assertFalse((self.root / ".git/index.lock").exists())

    def test_real_missing_identity_refused_without_configuration_changes(self):
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        self.git("config", "user.useConfigOnly", "true")
        before_index = (self.root / ".git/index").read_bytes()
        before_config = (self.root / ".git/config").read_bytes()
        without_identity = {key: value for key, value in os.environ.items()
                            if key not in {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                                           "GIT_COMMITTER_EMAIL", "EMAIL"}}
        without_identity.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        with patch.dict(os.environ, without_identity, clear=True):
            with self.assertRaises(CheckpointError):
                checkpoint(self.root, self.notebook, "No invented identity")
        self.assertEqual((self.root / ".git/index").read_bytes(), before_index)
        self.assertEqual((self.root / ".git/config").read_bytes(), before_config)

    def test_index_replacement_failure_rolls_back_head(self):
        from scripts import research_checkpoint as module
        before_head = self.git("rev-parse", "HEAD")
        before_index = (self.root / ".git/index").read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("simulated replacement failure")):
            with self.assertRaisesRegex(CheckpointError, "HEAD was rolled back"):
                checkpoint(self.root, self.notebook, "Recover commit transaction")
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)
        self.assertEqual((self.root / ".git/index").read_bytes(), before_index)
        self.assertFalse((self.root / ".git/index.lock").exists())

    def test_external_lock_and_active_merge_are_not_removed(self):
        lock = self.root / ".git/index.lock"
        lock.write_bytes(b"external process")
        with self.assertRaisesRegex(CheckpointError, "index.lock"):
            checkpoint(self.root, self.notebook, "Concurrent")
        self.assertEqual(lock.read_bytes(), b"external process")
        lock.unlink()
        (self.root / ".git/MERGE_HEAD").write_text("marker")
        with self.assertRaisesRegex(CheckpointError, "active Git operation"):
            checkpoint(self.root, self.notebook, "Merge")

    def test_unsafe_path_entries_are_refused(self):
        for value in ("../outside", "notebook/../unrelated.txt", "notebook/", ".git/config",
                      "tmp/note.md", "notebook/file.lock", "C:/outside", "/outside"):
            with self.subTest(path=value):
                original_names = self.names
                self.names = [value]
                self.configure()
                with self.assertRaises(CheckpointError):
                    checkpoint(self.root, self.notebook, "Unsafe path")
                self.names = original_names

    def test_symlink_path_refused_when_supported(self):
        linked = self.notebook / "link.md"
        try:
            linked.symlink_to(self.root / "unrelated.txt")
        except OSError:
            self.skipTest("Host does not permit test symlinks")
        self.names.append("notebook/link.md")
        self.configure()
        with self.assertRaisesRegex(CheckpointError, "Symlink"):
            checkpoint(self.root, self.notebook, "Symlink")


if __name__ == "__main__":
    unittest.main()
