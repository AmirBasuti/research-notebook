# Contributing

Run `python -B -m unittest discover -s tests -v` before proposing changes.
Use synthetic data and temporary repositories for tests. Never include real
notebooks, credentials, participant records, model weights, or local machine paths.

Changes to record validation, path containment, locking, or checkpoint behavior
need a regression test demonstrating the failure before the fix. Keep old
records immutable: corrections create a superseding record.

Describe the behavior change and its tests. Keep dependencies minimal.
