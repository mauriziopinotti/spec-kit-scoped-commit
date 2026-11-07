# Spec Kit Scoped Commit

**A Spec Kit 1.3.1+ extension for committing only the changes attributable to the active coding-agent conversation, even when the Git worktree contains unrelated work.** No push, no stash, no branch switching.

> **Status:** v0.1.0 development release. The Git helper has been tested against temporary local Git repositories. Installation with the real `specify` CLI and agent behavior must be verified in your environment before use on valuable worktrees.

## Behavior

1. The coding agent inspects *this conversation* and the full local Git status, index, untracked files and diffs.
2. It prepares a precise manifest of **whole files** and, where necessary, **individual diff hunks**. The agent must stop rather than guess about ambiguous ownership.
3. The helper previews the **exact resulting commit**, excluded paths/hunks, proposed commit message, and an approval token. The user approves that preview.
4. The helper constructs a Git tree in a temporary index, writes a local commit using Git plumbing, and adjusts the ordinary index **only for selected paths**. Unrelated staged changes remain staged and unrelated working-tree changes remain untouched.
5. If (and only if) this conversation completed the active Spec Kit feature, an optional close action clears the local `.specify/feature.json` pointer after a successful commit. Specifications and tasks remain in place, and the branch does not change.

The command does **not** automatically activate a Git hook and does **not** run on every implementation. You invoke it deliberately at the end of a chat.

## Install in an existing Spec Kit project

```sh
specify version  # should be >= 1.3.1
specify extension add --dev /path/to/spec-kit-scoped-commit
specify extension list
specify extension info scoped
```

With GitHub Copilot using the default skills layout, invoke it in Copilot chat as:

```text
/speckit-scoped-commit
```

Do not run that slash command in a terminal. Other agents may render the invocation differently.

## Example: overlapping work

```text
src/AuthService.java          chat A (whole file)
src/application.yml           chat A: hunk 2; chat B: hunk 1
src/PaymentService.java       chat B (already staged)
l10n/messages_en.arb          chat C (unstaged)
```

The agent prepares this manifest in a temporary file **outside the project working tree**:

```json
{
  "message": "fix(auth): handle expired sessions",
  "files": [
    {"path": "src/AuthService.java", "mode": "whole"},
    {"path": "src/application.yml", "mode": "hunks", "hunks": [2]}
  ],
  "close_feature": null
}
```

No path is included just because it is staged or because it belongs to the active feature. Hunk indices refer to the current output of `git diff HEAD -- <path>` and start at 1. Hunk mode is available only for tracked text files that have **no preexisting staged change in that same file**. If unrelated changes share the same Git hunk, the helper cannot safely split them and the agent must stop for a manual decision.

## What counts as "close the active feature"?

Spec Kit itself does not define a universal completion command. The local pointer `.specify/feature.json` contains the selected feature directory, for example:

```json
{"feature_directory":"specs/007-login"}
```

If the agent can confidently tie the current chat to that feature and all checklist items in that feature's `tasks.md` are checked, its manifest may request:

```json
"close_feature": "specs/007-login"
```

The helper refuses closure if any `- [ ]` task remains, the feature pointer changed, any changed file under that feature directory is omitted from the commit, the pointer is tracked in Git, or `SPECIFY_FEATURE_DIRECTORY` overrides the pointer. Once the commit succeeds, it deletes **only** the ignored local `.specify/feature.json`. It neither moves nor archives any spec document. It does not unset environment variables in the parent shell, nor does it switch Git branches.

If this conversation is unrelated to an active feature (e.g. small translation or bug fix), use `"close_feature": null` and leave the active feature alone.

## Helper commands (for debugging; agent normally runs these)

From the project root:

```sh
python3 .specify/extensions/scoped/scripts/scoped_commit.py inspect
python3 .specify/extensions/scoped/scripts/scoped_commit.py preview --manifest /tmp/my-chat-manifest.json
# Only after the user approves the exact preview:
python3 .specify/extensions/scoped/scripts/scoped_commit.py commit --manifest /tmp/my-chat-manifest.json --confirm <APPROVAL_TOKEN>
```

`preview` and `inspect` are read-only. The token binds the selected paths/hunks, resulting Git tree, message, HEAD and active-feature pointer (if closing). If anything relevant changes, commit is rejected and the user must approve a new preview.

## Safety and limitations

- **Chat attribution is performed by the coding agent.** Git does not know which chat produced a line. The Python tool can enforce only the manifest's explicit file/hunk scope; there is no reliable auto-detection of provenance.
- **Same-file changes require care:** partial hunk selection is supported, but not arbitrary line selection inside a single hunk. On ambiguity, do not commit.
- **Existing staged modifications in a partially selected file:** the operation fails instead of trying to rewrite staged hunks belonging to another chat. Unrelated staged *other files* are preserved.
- **No arbitrary staging:** Git selection happens in a temporary index; only selected paths are repaired in the ordinary index after the new HEAD is installed.
- **No remote operations:** there is no push command in this extension.
- **No normal commit hooks:** `git commit-tree` bypasses normal `pre-commit` and `commit-msg` hooks; run tests and any required validation before approval.
- **No submodules, symlinks, unmerged/conflicted paths or detached HEAD:** unsupported risky states fail closed. Concurrent mutation of the same worktree should also be avoided.
- **Feature closure is a local pointer reset,** not a `git merge`, `git branch -d` or archival convention.

## Tests

```sh
cd spec-kit-scoped-commit
python3 -m unittest discover -s tests -v
```

The test suite uses temporary Git repositories and checks that the commit includes only approved content, unrelated staged and unstaged changes survive, partial hunks stay separate, stale tokens are rejected, and features close only when ready.

## Files

```text
spec-kit-scoped-commit/
├── extension.yml
├── commands/commit.md
├── scripts/scoped_commit.py
├── tests/test_scoped_commit.py
├── README.md
├── LICENSE
└── CHANGELOG.md
```

## License

MIT.
