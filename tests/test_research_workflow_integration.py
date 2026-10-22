"""End-to-end CLI tests; fixtures never change the real repository's Git history."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'scripts/research.py'
NOTEBOOK = Path('notebook')
SCRATCH = ROOT / 'tmp/tests'


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-cli-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notebook = self.root / NOTEBOOK
        self.notebook.mkdir(parents=True)
        (self.notebook / 'README.md').write_text('# Fixture guide\n', encoding='utf-8')
        (self.notebook / '.gitattributes').write_text('* -text -filter -ident\n', encoding='utf-8')
        (self.notebook / 'inventory.json').write_text(json.dumps({
            'schema_version': 1, 'captured_at': '2026-10-02T20:00:00+03:30',
            'artifacts': [], 'coverage_gaps': ['No full conversations collected.']
        }), encoding='utf-8')
        self.body = self.root / 'body.md'
        self.body.write_text('# State\n\nThe sensor study is optional. a test workstation is user-reported.\n\n'
                             'Review requirements remain unresolved. Protocol review pending.\n',
                             encoding='utf-8')

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(TOOL), '--root', str(self.root), *args],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        return result

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True,
                                text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def add_state(self, record_id='first', *extra):
        return self.cli('record', '--id', record_id, '--type', 'state', '--title', 'Portable research state',
                        '--provenance', 'user', '--evidence-status', 'user-reported', '--outcome', 'pending',
                        '--body-file', str(self.body), *extra)

    def test_complete_cli_cycle_and_dynamic_record_checkpoint(self):
        self.git('init', '-q')
        self.git('config', 'user.name', 'Research Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        (self.root / 'unrelated.txt').write_text('original\n', encoding='utf-8')
        self.git('add', 'unrelated.txt')
        self.git('commit', '-qm', 'fixture baseline')
        (self.root / 'unrelated.txt').write_text('staged user work\n', encoding='utf-8')
        self.git('add', 'unrelated.txt')
        original_index_entry = self.git('ls-files', '-s', 'unrelated.txt')

        self.add_state()
        self.cli('build')
        self.cli('check')
        handoff = self.cli('context').stdout
        self.assertIn('a test workstation', handoff)
        self.assertIn('The sensor study is optional', handoff)
        self.assertIn('unresolved', handoff)
        self.assertIn('first', self.cli('search', 'workstation').stdout)
        static = ['README.md', '.gitattributes', 'inventory.json', 'ledger.jsonl',
                  'CURRENT.md', 'INDEX.md', 'checkpoint.json']
        (self.notebook / 'checkpoint.json').write_text(json.dumps({
            'schema_version': 1, 'managed_paths': [(NOTEBOOK / name).as_posix() for name in static]
        }), encoding='utf-8')
        first = json.loads(self.cli('checkpoint', '--message', 'fixture research checkpoint').stdout)
        self.assertEqual(first['status'], 'committed')
        self.assertFalse(first['pushed'])
        self.assertNotIn('unrelated.txt', first['files'])
        self.assertEqual(original_index_entry, self.git('ls-files', '-s', 'unrelated.txt'))
        self.assertEqual('original', self.git('show', 'HEAD:unrelated.txt'))

        self.add_state('second', '--supersedes', 'first')
        self.cli('build')
        second = json.loads(self.cli('checkpoint').stdout)
        self.assertIn((NOTEBOOK / 'records/second.md').as_posix(), second['files'])
        self.assertEqual(original_index_entry, self.git('ls-files', '-s', 'unrelated.txt'))
        self.assertEqual('unchanged', json.loads(self.cli('checkpoint').stdout)['status'])

        historical = self.notebook / 'records/first.md'
        historical.write_bytes(historical.read_bytes() + b'altered\n')
        before = self.git('rev-parse', 'HEAD')
        self.cli('checkpoint', success=False)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_writer_lock_excludes_a_second_process(self):
        # A real competing process holds the lock until this test releases stdin.
        code = ('import sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); from scripts.research import write_lock; '
                'nb=Path(sys.argv[2]); '\
                '\nwith write_lock(nb):\n print("READY",flush=True)\n sys.stdin.readline()\n')
        writer = subprocess.Popen([sys.executable, '-c', code, str(ROOT), str(self.notebook)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding='utf-8')
        try:
            self.assertEqual('READY', writer.stdout.readline().strip())
            refused = self.cli('record', '--type', 'session', '--title', 'Competing writer',
                               '--provenance', 'assistant', '--evidence-status', 'proposed',
                               '--outcome', 'pending', '--body-file', str(self.body), success=False)
            self.assertIn('locked', refused.stderr)
            self.assertFalse((self.notebook / 'ledger.jsonl').exists())
        finally:
            writer.communicate('\n', timeout=10)
        self.assertEqual(writer.returncode, 0)
        self.assertFalse((self.notebook / '.write.lock').exists())
        self.add_state()


if __name__ == '__main__':
    unittest.main()
