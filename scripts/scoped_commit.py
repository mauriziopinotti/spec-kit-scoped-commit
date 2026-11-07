#!/usr/bin/env python3
"""Commit a reviewed path/hunk scope without touching unrelated Git index/worktree state.

Designed for use by a Spec Kit coding agent. Chat attribution is NOT automated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass


class ScopeError(Exception):
    pass


def run(repo: Path, *args: str, env: dict | None = None, input_bytes: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=repo, env=env, input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise ScopeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def git_text(repo: Path, *args: str, env: dict | None = None) -> str:
    return run(repo, *args, env=env).stdout.decode("utf-8", errors="replace").strip()


def project_root() -> Path:
    here = Path.cwd().resolve()
    result = run(here, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise ScopeError("Not inside a Git repository")
    root = Path(result.stdout.decode().strip()).resolve()
    if here != root:
        raise ScopeError(f"Run this helper from repository root: {root}")
    if not (root / ".specify").is_dir():
        raise ScopeError("This is not an initialized Spec Kit project (missing .specify/)")
    return root


def git_path(repo: Path, relative: str) -> Path:
    value = git_text(repo, "rev-parse", "--git-path", relative)
    path = Path(value)
    return path if path.is_absolute() else (repo / path).resolve()


def head(repo: Path) -> str:
    if run(repo, "symbolic-ref", "-q", "HEAD", check=False).returncode:
        raise ScopeError("Detached HEAD is not supported")
    result = run(repo, "rev-parse", "--verify", "HEAD", check=False)
    if result.returncode:
        raise ScopeError("Repository has no initial commit")
    if run(repo, "ls-files", "--unmerged").stdout:
        raise ScopeError("Unmerged files detected; resolve conflicts first")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if git_path(repo, name).exists():
            raise ScopeError(f"Git operation in progress: {name}")
    return result.stdout.decode().strip()


def changed_paths(repo: Path) -> set[str]:
    tracked = run(repo, "diff", "--no-renames", "--name-only", "-z", "HEAD", "--").stdout
    untracked = run(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
    paths = (tracked + untracked).split(b"\0")
    return {os.fsdecode(p) for p in paths if p}


def validate_path(repo: Path, raw: object) -> str:
    if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw:
        raise ScopeError(f"Invalid path: {raw!r}")
    if any(c in raw for c in ("\n", "\r", "\0")):
        raise ScopeError("Newlines and NUL in paths are unsupported")
    pieces = raw.split("/")
    if any(part in ("", ".", "..", ".git") for part in pieces):
        raise ScopeError(f"Unsafe path: {raw!r}")
    if raw == ".specify/feature.json":
        raise ScopeError("feature.json is local state, never part of the commit")
    resolved = (repo / raw).resolve()
    if not resolved.is_relative_to(repo):
        raise ScopeError(f"Symlink escapes repository: {raw}")
    if any((repo / "/".join(pieces[:i])).is_symlink() for i in range(1, len(pieces) + 1)):
        raise ScopeError(f"Symlinks are unsupported in this release: {raw}")
    if (repo / raw).is_dir():
        raise ScopeError(f"Select individual files, not a directory: {raw}")
    return raw


def diff_bytes(repo: Path, path: str) -> bytes:
    return run(repo, "-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-textconv",
               "--no-renames", "--no-color", "--unified=3", "HEAD", "--", path).stdout


def parse_hunks(diff: bytes) -> tuple[bytes, list[bytes]]:
    lines = diff.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line.startswith(b"@@ -")]
    if not positions:
        return diff, []
    return b"".join(lines[:positions[0]]), [b"".join(lines[a:b]) for a, b in
            zip(positions, positions[1:] + [len(lines)])]


def load_manifest(repo: Path, filename: str) -> dict:
    try:
        manifest = json.loads(Path(filename).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScopeError(f"Cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ScopeError("Manifest must be an object")
    if set(manifest) - {"message", "files", "close_feature"}:
        raise ScopeError("Manifest contains unknown keys")
    message = manifest.get("message")
    if not isinstance(message, str) or not (message.strip() == message) or not message or "\0" in message:
        raise ScopeError("Provide a nonempty Git commit message")
    if len(message) > 500:
        raise ScopeError("Commit message is unusually long")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ScopeError("Manifest must include at least one file")
    seen: set[str] = set()
    normalized = []
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "mode", "hunks"}:
            raise ScopeError("Each files item needs path, mode and optional hunks only")
        path = validate_path(repo, item.get("path"))
        mode = item.get("mode")
        if mode not in ("whole", "hunks") or path in seen:
            raise ScopeError(f"Invalid mode or duplicate path: {path}")
        seen.add(path)
        if mode == "whole":
            if "hunks" in item:
                raise ScopeError(f"whole mode must not specify hunks: {path}")
            normalized.append({"path": path, "mode": mode})
        else:
            indices = item.get("hunks")
            if not isinstance(indices, list) or not indices or any(type(n) is not int or n < 1 for n in indices):
                raise ScopeError(f"Hunk indices must be positive integers for {path}")
            if sorted(set(indices)) != indices:
                raise ScopeError(f"Hunk indices must be unique and ascending for {path}")
            normalized.append({"path": path, "mode": mode, "hunks": indices})
    close = manifest.get("close_feature")
    if close is not None and (not isinstance(close, str) or not close.strip()):
        raise ScopeError("close_feature must be null or an expected feature directory")
    return {"message": message, "files": normalized, "close_feature": close}


def feature_check(repo: Path, manifest: dict, changed: set[str]) -> str | None:
    close = manifest["close_feature"]
    if close is None:
        return None
    if os.environ.get("SPECIFY_FEATURE_DIRECTORY"):
        raise ScopeError("Cannot close feature while SPECIFY_FEATURE_DIRECTORY override is set")
    pointer = repo / ".specify" / "feature.json"
    if pointer.is_symlink() or not pointer.is_file():
        raise ScopeError("Missing or unsafe .specify/feature.json")
    if run(repo, "ls-files", "--error-unmatch", "--", ".specify/feature.json", check=False).returncode == 0:
        raise ScopeError("Cannot clear a tracked .specify/feature.json; migrate to ignored local pointer first")
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        active = data["feature_directory"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ScopeError(f"Invalid feature pointer: {exc}") from exc
    if close != active:
        raise ScopeError(f"Active feature changed: expected {close!r}, current {active!r}")
    feature_path = Path(active)
    if not feature_path.is_absolute():
        feature_path = repo / feature_path
    feature_path = feature_path.resolve()
    if not feature_path.is_relative_to(repo) or feature_path == repo:
        raise ScopeError("Closing a feature outside the project is unsupported")
    tasks = feature_path / "tasks.md"
    if not tasks.is_file() or tasks.is_symlink():
        raise ScopeError("Cannot close feature without a local tasks.md")
    text = tasks.read_text(encoding="utf-8")
    pending = re.findall(r"^\s*-\s*\[\s\]", text, re.MULTILINE)
    complete = re.findall(r"^\s*-\s*\[[xX]\]", text, re.MULTILINE)
    if pending or not complete:
        raise ScopeError(f"Feature not complete: {len(pending)} unchecked tasks, {len(complete)} checked tasks")
    selected = {item["path"]: item for item in manifest["files"]}
    prefix = feature_path.relative_to(repo).as_posix() + "/"
    for path in changed:
        if path.startswith(prefix) and (path not in selected or selected[path]["mode"] != "whole"):
            raise ScopeError(f"Cannot close feature while changes in feature directory remain unselected: {path}")
    return hashlib.sha256(pointer.read_bytes()).hexdigest()


@dataclass
class Prepared:
    repo: Path
    base: str
    tree: str
    message: str
    token: str
    included: list[str]
    excluded: list[str]
    manifest: dict
    feature_hash: str | None


def index_env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(path)
    return env


def make_selection(repo: Path, manifest: dict, index_file: Path) -> Prepared:
    base = head(repo)
    selected = {item["path"] for item in manifest["files"]}
    changed = changed_paths(repo)
    if not selected.issubset(changed):
        raise ScopeError("Some selected paths have no current changes: " + ", ".join(sorted(selected - changed)))
    feature_hash = feature_check(repo, manifest, changed)
    env = index_env(index_file)
    run(repo, "read-tree", base, env=env)
    for item in manifest["files"]:
        path = item["path"]
        if item["mode"] == "whole":
            run(repo, "add", "-A", "--", path, env=env)
            continue
        # Do not overwrite already-staged unrelated data in a mixed file.
        if run(repo, "diff", "--cached", "--quiet", "HEAD", "--", path, check=False).returncode:
            raise ScopeError(f"Cannot select hunks from a path with pre-existing staged changes: {path}")
        raw = diff_bytes(repo, path)
        prefix, hunks = parse_hunks(raw)
        if not hunks or any(v in prefix for v in (b"new file mode", b"deleted file mode", b"old mode", b"new mode", b"rename from", b"rename to", b"Binary files", b"GIT binary patch")):
            raise ScopeError(f"Cannot partially stage a binary, renamed, newly added, deleted or mode-changed file: {path}")
        ids = item["hunks"]
        if ids[-1] > len(hunks):
            raise ScopeError(f"Hunk selection out of range for {path}; found {len(hunks)} hunks")
        patch = prefix + b"".join(hunks[i - 1] for i in ids)
        run(repo, "apply", "--cached", "--recount", "-", env=env, input_bytes=patch)
    tree = git_text(repo, "write-tree", env=env)
    included_raw = run(repo, "diff-tree", "--no-renames", "--no-commit-id", "--name-only", "-r", "-z", base, tree).stdout
    included = sorted(os.fsdecode(x) for x in included_raw.split(b"\0") if x)
    if set(included) != selected:
        raise ScopeError(f"Unexpected committed paths: actual={included}, requested={sorted(selected)}")
    payload = {"base": base, "tree": tree, "manifest": manifest, "feature_pointer_sha256": feature_hash}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    return Prepared(repo, base, tree, manifest["message"], token, included,
                    sorted(changed - selected), manifest, feature_hash)


def inspect(repo: Path) -> None:
    print("HEAD:", head(repo))
    pointer = repo / ".specify" / "feature.json"
    if pointer.is_file():
        try:
            print("Active feature:", json.loads(pointer.read_text()).get("feature_directory"))
        except (OSError, ValueError, TypeError):
            print("Active feature: invalid pointer")
    print("Git status (includes unrelated work):")
    print(run(repo, "status", "--short", "--untracked-files=all").stdout.decode(errors="replace").rstrip() or "(clean)")
    print("\nDiff hunks (1-based; HEAD -> working tree):")
    for path in sorted(changed_paths(repo)):
        raw = diff_bytes(repo, path)
        _, hunks = parse_hunks(raw)
        if not hunks:
            print(f"{path}: new, deleted, binary, or no text hunks (whole-file review only)")
        else:
            print(path)
            for n, hunk in enumerate(hunks, 1):
                summary = hunk.splitlines()[0].decode(errors="replace")
                print(f"  [{n}] {summary}")
    print("\nInspect contents and exact hunks with: git diff HEAD -- <path>")


def preview(prepared: Prepared, index_file: Path) -> None:
    print("SCOPED COMMIT PREVIEW (NO CHANGES MADE)")
    print("HEAD:", prepared.base)
    print("Message:", prepared.message)
    print("Included:")
    for item in prepared.manifest["files"]:
        detail = "whole file" if item["mode"] == "whole" else "hunks " + ", ".join(map(str, item["hunks"]))
        print(f"  + {item['path']} ({detail})")
    if prepared.excluded:
        print("Excluded changed paths (untouched):")
        for item in prepared.excluded:
            print(f"  - {item}")
    print("Close active feature:", prepared.manifest["close_feature"] or "No")
    for item in prepared.manifest["files"]:
        if item["mode"] == "hunks":
            _, all_hunks = parse_hunks(diff_bytes(prepared.repo, item["path"]))
            omitted = [i for i in range(1, len(all_hunks) + 1) if i not in item["hunks"]]
            if omitted:
                print(f"Excluded hunks in {item['path']} (remain in worktree): {omitted}")
    print("\nActual proposed diff (selected scope only):")
    patch = run(prepared.repo, "--no-pager", "diff", "--cached", "--no-ext-diff", "--no-renames",
                env=index_env(index_file)).stdout
    if len(patch) > 100_000:
        print(patch[:100_000].decode(errors="replace") + "\n[DIFF TRUNCATED; review paths manually]")
    else:
        print(patch.decode(errors="replace").rstrip())
    print("\nAPPROVAL TOKEN:", prepared.token)
    print("Only commit after the user explicitly approves THIS selection and token.")
    print("NOTE: Git pre-commit and commit-msg hooks are not invoked by this helper.")


def git_ls_tree_entry(repo: Path, tree: str, path: str) -> tuple[str, str] | None:
    rows = run(repo, "ls-tree", "-z", tree, "--", path).stdout.split(b"\0")
    for row in rows:
        if not row:
            continue
        metadata, entry = row.split(b"\t", 1)
        if os.fsdecode(entry) != path:
            continue
        mode, typ, obj = metadata.decode().split(" ")
        if typ != "blob":
            raise ScopeError(f"Not a blob: {path}")
        return mode, obj
    return None


def commit(prepared: Prepared, temp_dir: Path) -> str:
    repo = prepared.repo
    true_index = git_path(repo, "index")
    true_index.parent.mkdir(parents=True, exist_ok=True)
    lock_path = true_index.with_name(true_index.name + ".lock")
    lock_fd = None
    replacement = temp_dir / "replacement-index"
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ScopeError(f"Git index is locked: {lock_path}") from exc
        # Preserve ALL existing index entries except the explicitly selected paths.
        if true_index.exists():
            shutil.copy2(true_index, replacement)
        else:
            run(repo, "read-tree", prepared.base, env=index_env(replacement))
        env = index_env(replacement)
        for path in prepared.included:
            item = git_ls_tree_entry(repo, prepared.tree, path)
            if item is None:
                run(repo, "update-index", "--force-remove", "--", path, env=env)
            else:
                mode, oid = item
                run(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=env)
        # Re-verify HEAD and the current feature pointer before making a commit.
        if head(repo) != prepared.base:
            raise ScopeError("HEAD changed after the preview; re-review required")
        if prepared.feature_hash is not None:
            pointer = repo / ".specify" / "feature.json"
            if not pointer.is_file() or hashlib.sha256(pointer.read_bytes()).hexdigest() != prepared.feature_hash:
                raise ScopeError("Active feature pointer changed; re-review required")
        # commit-tree reads message from stdin; does not use unrelated staged state.
        new_commit = run(repo, "commit-tree", prepared.tree, "-p", prepared.base,
                         input_bytes=(prepared.message + "\n").encode("utf-8")).stdout.decode().strip()
        run(repo, "update-ref", "-m", f"scoped-commit: {prepared.message[:60]}",
            "HEAD", new_commit, prepared.base)
        # The lock prevents Git writers from changing the user's index during replacement.
        os.replace(replacement, true_index)
        if prepared.manifest["close_feature"] is not None:
            (repo / ".specify" / "feature.json").unlink()
        return new_commit
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("inspect")
    for verb in ("preview", "commit"):
        sub = subs.add_parser(verb)
        sub.add_argument("--manifest", required=True)
        if verb == "commit":
            sub.add_argument("--confirm", required=True, help="Exact token from the user-approved preview")
    args = parser.parse_args()
    try:
        repo = project_root()
        if args.action == "inspect":
            inspect(repo)
            return 0
        manifest = load_manifest(repo, args.manifest)
        with tempfile.TemporaryDirectory(prefix="spec-kit-scoped-commit-") as tmp:
            index_file = Path(tmp) / "selection-index"
            prepared = make_selection(repo, manifest, index_file)
            if args.action == "preview":
                preview(prepared, index_file)
                return 0
            if args.confirm != prepared.token:
                raise ScopeError("Approval token missing/stale/invalid; run preview and request approval again")
            new_commit = commit(prepared, Path(tmp))
            print("Local commit created:", new_commit)
            print("Files included:", ", ".join(prepared.included))
            print("Other changed paths preserved:", ", ".join(prepared.excluded) or "none")
            print("Feature pointer cleared:" , "yes" if manifest["close_feature"] else "no")
            print("No push was performed.")
        return 0
    except (ScopeError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
