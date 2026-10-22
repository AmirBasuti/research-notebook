"""Scoped local research commits without consuming unrelated staged work.

The notebook must already have passed its content checks and its writer lock
must be held by the caller. Git's index lock protects this transaction. Plumbing
commits deliberately do not run repository hooks, sign, push, or change identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


class CheckpointError(RuntimeError):
    """A checkpoint was refused or could not be safely completed."""


def _git(root: Path, *args: str, index: Path | None = None,
         data: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
    env = os.environ.copy()
    # Do not inherit an unrelated alternate index from a calling shell.
    env.pop("GIT_INDEX_FILE", None)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args],
                            input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode not in ok:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise CheckpointError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _safe_file(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise CheckpointError(f"Invalid managed path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(
            part in {".", "..", ".git", "tmp"} or part.endswith(".lock")
            for part in path.parts):
        raise CheckpointError(f"Unsafe managed path: {value!r}")
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root):
        raise CheckpointError(f"Managed path escapes repository: {value}")
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise CheckpointError(f"Symlink or junction is not a managed file: {value}")
    if not target.is_file():
        raise CheckpointError(f"Managed file is missing or not regular: {value}")
    return target


def _entries(root: Path, index: Path | None = None) -> dict[str, bytes]:
    entries = {}
    for row in _git(root, "ls-files", "--stage", "-z", index=index).split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        if stage != b"0":
            raise CheckpointError("Unmerged index entries must be resolved first.")
        entries[name.decode("utf-8", "surrogateescape")] = metadata
    return entries


def _tree_entries(root: Path, tree: str) -> dict[str, bytes]:
    entries = {}
    for row in _git(root, "ls-tree", "-r", "-z", tree).split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            entries[name.decode("utf-8", "surrogateescape")] = mode + b" " + oid + b" 0"
    return entries


def _verify_history(root: Path, notebook: str, head: str) -> None:
    entries = _tree_entries(root, head)
    ledger = notebook + "/ledger.jsonl"
    immutable = (notebook + "/records/", notebook + "/snapshots/")
    for name in entries:
        if name != ledger and not name.startswith(immutable):
            continue
        target = _safe_file(root, name)
        old = _git(root, "show", f"{head}:{name}")
        current = target.read_bytes()
        if name == ledger:
            if (old and not old.endswith(b"\n")) or not current.startswith(old):
                raise CheckpointError("Committed ledger history is not an exact append-only prefix.")
            if current and not current.endswith(b"\n"):
                raise CheckpointError("Ledger has an incomplete final line.")
        elif current != old:
            raise CheckpointError(f"Committed immutable history changed: {name}")


def _registered_files(root: Path, notebook: Path) -> list[str]:
    """Expand only exact paths registered in the validated ledger/inventory."""
    notebook_name = notebook.relative_to(root).as_posix()
    registered = []
    try:
        ledger = notebook / "ledger.jsonl"
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                name = entry.get("path")
                if not isinstance(name, str) or not name.startswith("records/"):
                    raise CheckpointError("Registered ledger paths must be exact files under records/.")
                registered.append(notebook_name + "/" + name)
        inventory = notebook / "inventory.json"
        if inventory.exists():
            entries = json.loads(inventory.read_text(encoding="utf-8")).get("artifacts", [])
            if not isinstance(entries, list):
                raise CheckpointError("Inventory artifacts must be a list.")
            for entry in entries:
                name = entry.get("snapshot_path")
                if name is None:
                    continue
                if not isinstance(name, str) or not name.startswith("snapshots/"):
                    raise CheckpointError("Registered snapshots must be exact files under snapshots/.")
                registered.append(notebook_name + "/" + name)
    except (OSError, ValueError, AttributeError) as exc:
        raise CheckpointError(f"Cannot read registered checkpoint paths: {exc}") from exc
    return registered


def checkpoint(root: Path, notebook: Path, message: str) -> dict:
    """Commit exact configured files locally, preserving every unrelated index entry.

    A staged managed file is accepted only when its staged content equals the
    current working content. A partially staged managed edit requires the user's
    own resolution first. Missing identity and Git failures do not modify HEAD
    or the real index. No hooks or external publication are performed.
    """
    root = root.resolve()
    notebook = notebook.resolve()
    if not notebook.is_relative_to(root):
        raise CheckpointError("Notebook must be inside the repository.")
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise CheckpointError("A nonempty commit message is required.")
    actual_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual_root != root:
        raise CheckpointError("root must be the Git worktree root.")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").decode().strip())
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
                   "rebase-apply", "sequencer", "BISECT_LOG"):
        if (git_dir / marker).exists():
            raise CheckpointError(f"Finish the active Git operation first: {marker}")
    try:
        config = json.loads((notebook / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"Cannot read checkpoint configuration: {exc}") from exc
    names = config.get("managed_paths")
    if config.get("schema_version") != 1 or not isinstance(names, list) or not names:
        raise CheckpointError("checkpoint.json needs schema_version 1 and explicit managed_paths.")
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise CheckpointError("Managed paths must be distinct strings.")
    names = sorted(set(names).union(_registered_files(root, notebook)))
    paths = {name: _safe_file(root, name) for name in names}
    before_bytes = {name: hashlib.sha256(path.read_bytes()).digest() for name, path in paths.items()}
    # Check identity before touching any index or creating a commit object.
    _git(root, "var", "GIT_AUTHOR_IDENT")
    _git(root, "var", "GIT_COMMITTER_IDENT")
    head = _git(root, "rev-parse", "--verify", "HEAD", ok=(0, 128)).decode().strip()
    if not head:
        raise CheckpointError("Initialize the repository with a deliberate first commit before checkpointing.")
    notebook_name = notebook.relative_to(root).as_posix()
    _verify_history(root, notebook_name, head)
    ref = _git(root, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).decode().strip() or "HEAD"
    index_file = git_dir / "index"
    index_lock = git_dir / "index.lock"
    try:
        lock_fd = os.open(index_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CheckpointError("Git index.lock already exists; another process or interrupted operation needs attention.") from exc
    os.close(lock_fd)
    owns_lock = True
    try:
        original_index = index_file.read_bytes() if index_file.exists() else b""
        original_entries = _entries(root)
        head_entries = _tree_entries(root, head)
        for name in names:
            staged = original_entries.get(name)
            if staged is not None and staged != head_entries.get(name):
                working_oid = _git(root, "hash-object", "--path", name, "--", name).strip()
                if staged.split()[1] != working_oid:
                    raise CheckpointError(f"Partially staged managed file requires explicit resolution: {name}")
            elif staged is None and name in head_entries:
                raise CheckpointError(f"Managed file has a staged deletion: {name}")
        with tempfile.TemporaryDirectory(prefix="research-checkpoint-", dir=git_dir) as temporary:
            commit_index = Path(temporary) / "commit.index"
            next_index = Path(temporary) / "next.index"
            _git(root, "read-tree", head, index=commit_index)
            nul_paths = b"\0".join(name.encode("utf-8") for name in names) + b"\0"
            _git(root, "add", "--pathspec-from-file=-", "--pathspec-file-nul",
                 index=commit_index, data=nul_paths)
            changed = [name.decode("utf-8", "surrogateescape") for name in
                       _git(root, "diff", "--cached", "--name-only", "-z", head,
                            index=commit_index).split(b"\0") if name]
            if not set(changed).issubset(names):
                raise CheckpointError("Prepared commit unexpectedly includes an unmanaged path.")
            for name, path in paths.items():
                if hashlib.sha256(path.read_bytes()).digest() != before_bytes[name]:
                    raise CheckpointError(f"Managed file changed during checkpoint: {name}")
            if not changed:
                return {"status": "unchanged", "commit": head, "files": [],
                        "index_preserved": True, "pushed": False}
            tree = _git(root, "write-tree", index=commit_index).decode().strip()
            commit = _git(root, "commit-tree", tree, "-p", head,
                          data=(message.strip() + "\n").encode("utf-8")).decode().strip()
            if original_index:
                next_index.write_bytes(original_index)
            else:
                _git(root, "read-tree", "--empty", index=next_index)
            committed_entries = _entries(root, commit_index)
            updates = b"".join(committed_entries[name] + b"\t" + name.encode("utf-8") + b"\0"
                               for name in names)
            _git(root, "update-index", "-z", "--index-info", index=next_index, data=updates)
            next_entries = _entries(root, next_index)
            outside = lambda entries: {k: v for k, v in entries.items() if k not in paths}
            if outside(next_entries) != outside(original_entries):
                raise CheckpointError("Prepared index changed unrelated staged entries.")
            if (index_file.read_bytes() if index_file.exists() else b"") != original_index:
                raise CheckpointError("Real index changed despite its lock; checkpoint refused.")
            current_ref = _git(root, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).decode().strip() or "HEAD"
            if ref != current_ref:
                raise CheckpointError("Current branch changed during checkpoint.")
            # Prepare the replacement before moving HEAD. index.lock remains
            # a recoverable index if the process is interrupted at this point.
            with index_lock.open("wb") as output:
                output.write(next_index.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            _git(root, "update-ref", "-m", "research checkpoint", ref, commit, head)
            try:
                os.replace(index_lock, index_file)
                owns_lock = False
            except OSError as exc:
                try:
                    _git(root, "update-ref", "-m", "rollback failed research checkpoint", ref, head, commit)
                except CheckpointError:
                    owns_lock = False  # Retain the recoverable replacement index.
                    raise CheckpointError(
                        f"HEAD advanced to {commit}, but index replacement failed. Retained {index_lock}; "
                        "inspect it before any further Git operation.") from exc
                raise CheckpointError("Index replacement failed; HEAD was rolled back.") from exc
            return {"status": "committed", "commit": commit, "files": changed,
                    "index_preserved": True, "pushed": False}
    finally:
        if owns_lock:
            index_lock.unlink(missing_ok=True)
