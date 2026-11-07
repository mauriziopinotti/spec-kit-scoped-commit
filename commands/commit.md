---
description: "Commit only changes related to this chat, preserving every unrelated worktree/index change; optionally close the completed active feature."
---

# Scoped Commit — current conversation only

User arguments: `$ARGUMENTS`

You are executing a potentially destructive Git action. The user's current conversation is the source of intent, NOT the active feature pointer or a `git status` file list.

## Mandatory safety requirements

- NEVER `git add .`, `git add -A` without an explicit path, `git stash`, `git reset`, `git checkout`, `git restore`, `git clean`, `git push`, `git switch`, `git merge`, or `git rebase`.
- Do not use `git commit` directly. Use the provided deterministic Python helper after explicit approval.
- Changes in the working tree or index that are not attributable to THIS conversation are out of scope, even if their file paths belong to the active Spec Kit feature.
- A file may contain changes from multiple conversations. Include only relevant diff HUNKS; never treat the whole file as relevant without reviewing all its modifications.
- If the intended diff cannot be reliably separated by ordinary Git hunks (e.g. adjacent edits share one hunk), STOP and explain the ambiguity. Do not force or invent a patch.
- If attribution is unclear, ask the user; do not guess. Never infer that a modification belongs to this chat solely from its timestamp, location, Git status, or current active feature.
- Never commit a secret, credential, generated build artifact, or file ignored by Git unless the user separately and explicitly authorizes it (helper intentionally does not force-add ignored files).
- Do not silently include previously staged unrelated changes. Existing unrelated staged changes must remain staged and untouched.
- Do not amend, create branches, run push, or change remotes.
- If there is no attributable change, explain and do not commit.

## Procedure

1. Work from the Git repository root containing `.specify/`. Locate `.specify/extensions/scoped/scripts/scoped_commit.py`. Do not modify project code, tasks, or spec artifacts merely to make a commit.
2. Read the conversation context to identify exactly what THIS conversation implemented. Run `python3 .specify/extensions/scoped/scripts/scoped_commit.py inspect` and inspect relevant diffs (`git diff HEAD -- <path>`), untracked file content, existing staged changes, and any same-file hunk boundaries. Use `inspect` output for 1-based hunk indices.
3. Create a temporary JSON manifest outside the worktree, such as `/tmp/scoped-commit-chat-<unique-id>.json`. Include only intended paths. Example:

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

   The `whole` mode takes the file's current working-tree version, including any staged changes for that file. Choose it only when ALL differences from HEAD are attributable to the current conversation. `hunks` selects exact, 1-based hunks from `git diff HEAD` for a tracked text file with a clean index entry; use it when the file includes unrelated work. A single Git hunk is indivisible in this release.

4. Determine if the CURRENT CHAT's work implements the active Spec Kit feature (not merely whether a feature is selected). The active feature is in `.specify/feature.json`, or overridden by `SPECIFY_FEATURE_DIRECTORY`. If **not** directly related, leave `close_feature` as `null` regardless of whether there is an active feature. If directly related and the user wants to finish it, set `close_feature` to the EXACT `feature_directory` string from `.specify/feature.json`. The helper will refuse to close if tasks are incomplete, the feature pointer changes, or feature artifacts are modified outside the selected scope. Never delete specifications, change branches, or archive files.
5. Run:

   ```sh
   python3 .specify/extensions/scoped/scripts/scoped_commit.py preview --manifest /tmp/scoped-commit-chat-<unique-id>.json
   ```

6. Present the commit message, included files/hunks, omitted files, and whether the feature will close. Quote the approval token printed by preview. Obtain explicit user approval of that EXACT preview before proceeding. A general instruction to "commit" is not approval of a newly constructed scope when other work is mixed.
7. Only after approval, execute:

   ```sh
   python3 .specify/extensions/scoped/scripts/scoped_commit.py commit --manifest /tmp/scoped-commit-chat-<unique-id>.json --confirm <TOKEN_FROM_PREVIEW>
   ```

   If the preview becomes stale or any check fails, STOP; re-preview and ask for approval again.
8. Report commit SHA, files included, excluded changes untouched, and closure status. The helper never pushes.

## Definition of closing the feature

Spec Kit has no core "close spec" command. A successful closure simply removes the checkout-local `.specify/feature.json` pointer **after** a successful commit, only when it still names the expected active feature and every checklist task in `tasks.md` is complete. It does not remove or relocate the spec artifacts. If `SPECIFY_FEATURE_DIRECTORY` overrides the active pointer, the helper refuses closure: a child process cannot clear the parent shell's environment variable.

## Limitations

The agent, not the Git helper, attributes edits to a chat. The helper enforces the explicitly selected file/hunk boundaries, cannot infer chat provenance, and cannot split one Git hunk. It uses Git plumbing (`commit-tree`) to ensure a clean scope and preserve unrelated staging, so normal `pre-commit` / `commit-msg` hooks are NOT executed. Run the project's validation checks yourself before approval.
