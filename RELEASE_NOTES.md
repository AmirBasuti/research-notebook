# Initial standalone release

Extracted the research-record CLI and its tests into a standalone package.
The default notebook directory is now `notebook/`; `--notebook` still selects
another repository-relative directory. Examples and test fixtures are synthetic.
Integrity validation and Git checkpoint transaction behavior are unchanged.

This release contains tooling only: no private research records, datasets,
participant identifiers, model weights, or experiment results.
