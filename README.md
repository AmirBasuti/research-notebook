# Research Notebook

Keep a readable research history without silently rewriting earlier decisions.

Research Notebook is a small Python command-line tool for recording questions,
decisions, sources, experiments and current state. It checks record hashes and
relationships, builds readable Markdown views, and can create carefully scoped
local Git checkpoints while preserving unrelated staged work.

It uses the Python standard library. There is no service, account, database,
model or network dependency. Git is required only for checkpoints and their tests.

## Five-minute example

Run these commands from the repository root with Python 3.11 or newer. The local
release was tested on Python 3.14.3; the included CI matrix is configured for
3.11–3.14 on Linux and Windows, but those remote jobs have not yet run.

```sh
python -c "import shutil; shutil.copytree('examples/demo', 'demo')"
python scripts/research.py --root demo record --id first-state --type state --title "Synthetic sensor study" --provenance user --evidence-status proposed --outcome pending --body-file demo/body.md
python scripts/research.py --root demo build
python scripts/research.py --root demo check
python scripts/research.py --root demo context
python scripts/research.py --root demo search sensor
```

The example creates `demo/notebook/`. Open `CURRENT.md` for the latest state and
`INDEX.md` for the timeline. The sample is fictional and contains no research
measurements. The copy command refuses an existing `demo` directory; use a new
destination when repeating it.

To record a correction, write a new body and register it with
`--supersedes first-state`. To link a follow-up without replacing a state, use
`--related first-state`. Record bodies are Markdown; `--reference` accepts an
existing repository-relative file or an HTTPS URL. Remote URLs are recorded but
never fetched or verified by the checker.

## Commands

Global options precede the subcommand:

```text
python scripts/research.py --root <repository> --notebook notebook <command>
```

| Command | Purpose |
|---|---|
| `record` | Create a new immutable Markdown record and append its registration |
| `build` | Rebuild `CURRENT.md` and `INDEX.md` from registered records |
| `check` | Verify metadata, hashes, references, snapshots and generated views |
| `search` | Find records by text, topic, type or evidence status |
| `context` | Produce a bounded, current-state-first handoff |
| `checkpoint` | Commit only explicitly managed files to local Git |

Use `python scripts/research.py record --help` for record fields and
`python scripts/research.py --help` for the full command list. There is no `init`
command: the example supplies the small initial inventory and guide.

Records distinguish **user-reported**, **source-reported**, **locally-verified**,
**proposed** and **unresolved** evidence. Outcomes are tracked separately as
accepted, rejected, exploratory, pending or not applicable. This helps keep a
proposal, a reported score and a locally verified result from becoming the same
claim.

## What is stored

```text
notebook/
  README.md          Your workflow guide
  inventory.json     Captured-artifact metadata and known coverage gaps
  ledger.jsonl       Append-only record metadata and hashes
  records/           Immutable Markdown records
  snapshots/         Optional captured artifact bytes
  CURRENT.md         Generated latest-state view
  INDEX.md           Generated timeline and artifact index
  checkpoint.json   Optional explicit Git checkpoint allowlist
```

The tool does not automatically capture source artifacts or whole conversations.
If you add inventory entries, provide their matching snapshot bytes and hashes.
Keep private material out of public notebooks and repository history.

## Optional local Git checkpoints

Checkpointing requires an existing Git repository with a deliberate initial
commit and a configured Git identity. First run `build` and `check`. Then add
`notebook/checkpoint.json` containing only the files you intend to manage:

```json
{
  "schema_version": 1,
  "managed_paths": [
    "notebook/README.md",
    "notebook/.gitattributes",
    "notebook/inventory.json",
    "notebook/ledger.jsonl",
    "notebook/CURRENT.md",
    "notebook/INDEX.md",
    "notebook/checkpoint.json"
  ]
}
```

Registered records and inventory snapshots are added individually. The command
does not sweep arbitrary directories. The supplied notebook `.gitattributes`
preserves exact stored bytes across Git checkouts, which matters for hashes.
The package ignores its default working `notebook/`; adjust your repository's
ignore rules deliberately if you want ordinary Git commands to track it.

```sh
python scripts/research.py --root <repository> --notebook notebook checkpoint --message "Record validated research state"
```

A checkpoint creates a local commit and updates the branch. It never pushes.
It refuses partially staged managed files, an active merge/rebase, an existing
index lock and edits to committed immutable history. Tests exercise preservation
of unrelated staged, unstaged and untracked files, as well as transaction failure
and rollback behavior. Checkpoint plumbing intentionally does not run Git hooks
or sign commits; use your normal Git workflow if those are required.

## Tests

```sh
python -B -m unittest discover -s tests -v
```

Tests use synthetic notebooks and temporary Git repositories. Some symlink tests
skip when the operating system does not permit creating the required links.
The suite covers integrity failures, interrupted registration, competing writers,
path redirection, UTF-8 output, deterministic views and scoped Git transactions.

## Limits

- Hashes detect changes relative to stored metadata; this is not tamper-proof or
  cryptographically signed storage.
- Local Git history is recoverability, not an off-device backup.
- Markdown link parsing is intentionally lightweight, not a full Markdown parser.
- Validation does not establish scientific validity, dataset consent, or the
  truth of a source's claims.
- A crashed writer can leave a lock. Inspect its host/PID and preserve evidence
  before manually recovering it; the tool never automatically breaks a lock.
- Cross-platform CI is configured, not a claim of completed remote validation.

## License

MIT. Copyright © 2026 Amir Basuti. The license applies to this tooling package;
no datasets, model implementations or research records are distributed with it.
