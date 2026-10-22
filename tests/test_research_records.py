"""Integrity, interruption, and handoff checks using isolated notebooks."""
from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("research", Path(__file__).resolve().parents[1] / "scripts/research.py")
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


class ResearchRecordsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.notebook = self.root / research.NOTEBOOK_REL
        self.notebook.mkdir(parents=True)
        (self.notebook / "README.md").write_text("# Workflow\n", encoding="utf-8")
        self.inventory = {"schema_version": 1, "captured_at": research.utc_now(), "artifacts": [], "coverage_gaps": ["Reconstructed, not complete chat history."]}
        self.save_inventory()

    def save_inventory(self):
        (self.notebook / "inventory.json").write_text(json.dumps(self.inventory), encoding="utf-8")

    def add(self, record_id="first", record_type="state", body="# Current\n\nThe sensor study is optional. a test workstation is user-reported. Review requirements unresolved.\n", **fields):
        metadata = {"id": record_id, "type": record_type, "title": "Test research record", "provenance": "user", "evidence_status": "user-reported", "outcome": "pending", "topics": ["thesis"], **fields}
        with research.write_lock(self.notebook):
            return research.record(self.root, self.notebook, metadata, body)

    def build(self):
        with research.write_lock(self.notebook):
            research.build_views(self.root, self.notebook)

    def test_complete_roundtrip_and_deterministic_build(self):
        entry = self.add()
        self.build()
        before = {name: (self.notebook / name).read_bytes() for name in ("CURRENT.md", "INDEX.md")}
        self.build()
        self.assertEqual(before, {name: (self.notebook / name).read_bytes() for name in before})
        self.assertEqual([], research.validate(self.root, self.notebook))
        self.assertEqual(entry["entry_sha256"], research.entry_digest(entry))
        context = research.context_text(self.root, self.notebook)
        self.assertIn("The sensor study is optional", context)
        self.assertIn("a test workstation", context)
        self.assertIn("Review requirements unresolved", context)
        self.assertEqual(1, len(research.matching_entries(self.notebook, "workstation", ["thesis"])))

    def test_historical_edit_and_deletion_detected(self):
        self.add()
        self.build()
        path = self.notebook / "records/first.md"
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        self.assertTrue(any("content hash mismatch" in error for error in research.validate(self.root, self.notebook)))
        path.unlink()
        self.assertTrue(any("cannot read record" in error for error in research.validate(self.root, self.notebook)))

    def test_correction_requires_explicit_supersession(self):
        self.add()
        original = (self.notebook / "records/first.md").read_bytes()
        with self.assertRaisesRegex(research.ResearchError, "must supersede"):
            self.add("second")
        self.add("second", supersedes=["first"], body="# Updated\n\nNew evidence is explicitly recorded.\n")
        self.build()
        self.assertEqual(original, (self.notebook / "records/first.md").read_bytes())
        self.assertIn("New evidence", (self.notebook / "CURRENT.md").read_text(encoding="utf-8"))
        self.assertEqual([], research.validate(self.root, self.notebook))

    def test_duplicate_id_unknown_relation_and_bad_link_rejected_before_write(self):
        self.add()
        original = (self.notebook / "ledger.jsonl").read_bytes()
        with self.assertRaisesRegex(research.ResearchError, "cannot be overwritten"):
            self.add()
        with self.assertRaisesRegex(research.ResearchError, "unknown or future"):
            self.add("second", "decision", related_ids=["missing"])
        with self.assertRaisesRegex(research.ResearchError, "broken Markdown"):
            self.add("third", "source", body="[Missing](missing.md)")
        self.assertEqual(original, (self.notebook / "ledger.jsonl").read_bytes())
        self.assertFalse((self.notebook / "records/second.md").exists())

    def test_orphan_and_truncated_append_block_further_writes(self):
        self.add()
        orphan = self.notebook / "records/orphan.md"
        orphan.write_text("interrupted write", encoding="utf-8")
        with self.assertRaisesRegex(research.ResearchError, "orphan"):
            self.add("next", "session")
        orphan.unlink()
        ledger = self.notebook / "ledger.jsonl"
        ledger.write_bytes(ledger.read_bytes() + b'{"id":')
        errors = research.validate(self.root, self.notebook)
        self.assertTrue(any("interrupted append" in error for error in errors))
        with self.assertRaises(research.ResearchError):
            self.add("next", "session")

    def test_writer_lock_excludes_concurrent_writer_and_reports_stale_lock(self):
        with research.write_lock(self.notebook):
            with self.assertRaisesRegex(research.ResearchError, "locked"):
                with research.write_lock(self.notebook):
                    pass
            self.assertTrue(any("writer lock" in error for error in research.validate(self.root, self.notebook)))
        self.assertFalse((self.notebook / ".write.lock").exists())

    def test_hash_chain_break_and_duplicate_registration_detected(self):
        self.add()
        self.add("second", "decision")
        entries, _ = research.load_ledger(self.notebook)
        entries[1]["previous_entry_sha256"] = "0" * 64
        entries[1]["entry_sha256"] = research.entry_digest(entries[1])
        ledger = self.notebook / "ledger.jsonl"
        ledger.write_bytes(b"".join(research.canonical(entry) + b"\n" for entry in entries))
        self.assertTrue(any("hash chain" in error for error in research.validate(self.root, self.notebook)))
        ledger.write_bytes(ledger.read_bytes() + research.canonical(entries[0]) + b"\n")
        self.assertTrue(any("duplicate ID" in error for error in research.validate(self.root, self.notebook)))

    def test_snapshot_verification_and_restore(self):
        source = self.root / "old.md"
        source.write_bytes(b"Historical result: rejected.\r\n")
        snapshot = self.notebook / "snapshots/old.md"
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        item = {"id": "old", "source_path": "old.md", "snapshot_path": "snapshots/old.md", "external": False, "mutable_source": True, "category": "history", "event_date": "2026-09-13", "sha256": research.sha256(snapshot.read_bytes()), "bytes": snapshot.stat().st_size}
        self.inventory["artifacts"] = [item]
        self.save_inventory()
        self.add()
        self.build()
        source.write_text("Current file changed", encoding="utf-8")
        self.assertEqual([], research.validate(self.root, self.notebook))
        restored = self.root / "restored.md"
        restored.write_bytes(snapshot.read_bytes())
        self.assertEqual(item["sha256"], research.sha256(restored.read_bytes()))
        snapshot.write_text("bad copy", encoding="utf-8")
        self.assertTrue(any("snapshot bytes/hash mismatch" in error for error in research.validate(self.root, self.notebook)))

    def test_fenced_examples_and_relative_state_links(self):
        linked = self.notebook / "source.md"
        linked.write_text("# Source\n", encoding="utf-8")
        self.add(body="# State\n\n[Source](../source.md)\n\n```markdown\n[example](missing.md)\n```\n")
        self.build()
        self.assertEqual([], research.validate(self.root, self.notebook))
        self.assertIn("](source.md)", (self.notebook / "CURRENT.md").read_text(encoding="utf-8"))
        handoff = research.context_text(self.root, self.notebook)
        self.assertIn(f"]({research.NOTEBOOK_REL.as_posix()}/source.md)", handoff)

    def test_stale_generated_and_missing_reference_detected(self):
        source = self.root / "evidence.md"
        source.write_text("# Evidence", encoding="utf-8")
        self.add(references=["evidence.md", "https://example.org/paper"])
        self.build()
        (self.notebook / "CURRENT.md").write_text("outdated", encoding="utf-8")
        self.assertTrue(any("stale generated" in error for error in research.validate(self.root, self.notebook)))
        source.unlink()
        self.assertTrue(any("missing reference" in error for error in research.validate(self.root, self.notebook)))

    def test_bounded_context(self):
        body = "# State\n\n" + "\n\n".join("Paragraph " + "word " * 100 for _ in range(40))
        self.add(body=body)
        self.build()
        text = research.context_text(self.root, self.notebook, max_words=300)
        self.assertLessEqual(len(text.split()), 300)
        self.assertIn("truncated", text)

    def test_invalid_metadata_and_traversal(self):
        with self.assertRaises(research.ResearchError):
            self.add("../outside")
        with self.assertRaises(research.ResearchError):
            self.add(event_date="yesterday")
        with self.assertRaises(research.ResearchError):
            self.add(evidence_status="proved")
        with self.assertRaises(research.ResearchError):
            self.add(title=None)
        with self.assertRaises(research.ResearchError):
            research.contained(self.root, "../escape")
        self.assertFalse((self.notebook / "ledger.jsonl").exists())

    def directory_link(self, link, target):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Host does not allow directory symlinks")
            process = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            if process.returncode:
                self.skipTest("Host does not allow directory symlinks or junctions")
        # Unlink the redirection itself before TemporaryDirectory cleanup can
        # encounter it. Neither operation recursively deletes the destination.
        def remove_link():
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                link.rmdir()
        self.addCleanup(remove_link)

    def test_redirected_records_directory_rejected_before_write(self):
        outside = self.root / "outside-notebook"
        outside.mkdir()
        self.directory_link(self.notebook / "records", outside)
        with self.assertRaisesRegex(research.ResearchError, "escapes|redirected"):
            self.add()
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((self.notebook / "ledger.jsonl").exists())

    def test_internal_directory_redirection_also_rejected(self):
        target = self.notebook / "alternate-records"
        target.mkdir()
        self.directory_link(self.notebook / "records", target)
        with self.assertRaisesRegex(research.ResearchError, "redirected"):
            self.add()
        self.assertEqual([], list(target.iterdir()))

    def test_generated_destination_redirection_preserves_target(self):
        self.add()
        self.build()
        destination = self.notebook / "CURRENT.md"
        destination.unlink()
        target = self.root / "protected.md"
        target.write_bytes(b"do not modify")
        try:
            os.symlink(target, destination)
        except OSError:
            self.skipTest("Host does not allow file symlinks")
        self.addCleanup(destination.unlink)
        with self.assertRaisesRegex(research.ResearchError, "escapes|redirected"):
            self.build()
        self.assertEqual(b"do not modify", target.read_bytes())

    def test_cli_handoff_is_utf8_under_legacy_windows_encoding(self):
        self.add(body="# Current\n\nپژوهش — sources F1–F6 remain provisional.\n")
        self.build()
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        process = subprocess.run([sys.executable, str(Path(research.__file__)), "--root", str(self.root), "context"],
                                 capture_output=True, env=environment)
        self.assertEqual(0, process.returncode, process.stderr.decode("utf-8", errors="replace"))
        output = process.stdout.decode("utf-8")
        self.assertIn("پژوهش — sources F1–F6", output)

    def test_check_cli_returns_failure_without_mutation(self):
        error = io.StringIO()
        with redirect_stderr(error):
            result = research.main(["--root", str(self.root), "check"])
        self.assertEqual(1, result)
        self.assertIn("No state record", error.getvalue())
        self.assertFalse((self.notebook / ".write.lock").exists())


if __name__ == "__main__":
    unittest.main()
