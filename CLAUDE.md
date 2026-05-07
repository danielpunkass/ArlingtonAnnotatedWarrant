# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This repo is a **public mirror** of the Arlington Town Meeting Annotated Warrant from `arlingtonma.primegov.com`. The canonical sync logic lives in a separate private parent project (`Town Meeting/`); this repo holds a vendored copy of `SyncAnnotatedWarrant.py` (with `ARCHIVE_DIR = "."` so it writes to the repo root) plus a GitHub Actions workflow that runs the sync on a schedule and pushes changes.

The Annotated Warrant is a *living* document on primegov: articles, descriptions, sponsors, and supporting attachments evolve as Town Meeting approaches and proceeds. The sync is designed to be re-run repeatedly and only commit real changes.

## Commands

```bash
# Run the sync locally (writes into repo root; safe to re-run)
python3 SyncAnnotatedWarrant.py
```

No tests, no build step, no dependencies beyond the Python 3 standard library. The same command runs in CI.

## Architecture

**Sync model.** Each attachment on primegov has an immutable `historyId`. `index.json` is the manifest mapping `(article itemId → list of attachments with historyId + local filename)`. On each run, `SyncAnnotatedWarrant.py`:

1. Fetches the meeting page HTML and writes it to `source.html` (gitignored — large, noisy).
2. Parses 95-ish article blocks out of the HTML via regex (no parser dependency on purpose).
3. For each attachment, downloads only if its `historyId` is new or changed; otherwise reuses the existing local file.
4. Removes orphan files (attachments no longer referenced) and orphan article directories (articles removed from the warrant).
5. Regenerates `INDEX.md`, per-article `article.md` files, and `index.json` with a new `lastSynced` timestamp.

**No-op suppression.** The script reads the prior `index.json` before writing; if everything except `lastSynced` would be identical, it reuses the prior timestamp. This means a sync run with no upstream changes leaves the working tree clean, and the workflow's `git status --porcelain` check naturally short-circuits the commit. Don't reintroduce an unconditional timestamp bump — it would generate a commit every 30 minutes even when nothing changed.

**Vendored script drift.** This repo's `SyncAnnotatedWarrant.py` is a copy of the canonical version in the parent TMM project. The only intended difference is `ARCHIVE_DIR = "."`. When the canonical script is updated, this copy needs to be updated too. If the divergence becomes painful, candidates: (a) make `ARCHIVE_DIR` env-var-driven in both copies so the file becomes byte-identical, or (b) move the canonical script here and have the parent project invoke it from a submodule/clone.

## CI / Automation

`.github/workflows/sync.yml` runs every 30 minutes (cron `*/30 * * * *`, UTC, best-effort — GitHub often delays scheduled runs by 5–15 minutes under load) and on manual dispatch. It uses the default `GITHUB_TOKEN` with `permissions: contents: write` to push back to `main`. No PAT needed because the workflow lives in the same repo it pushes to.

**Concurrency.** `concurrency.group: sync` with `cancel-in-progress: false` ensures overlapping runs queue rather than racing — important because two simultaneous pushes to `main` would conflict.

**Repo-level setting required.** *Settings → Actions → General → Workflow permissions* must be set to "Read and write permissions" for the workflow's push step to succeed. If a future run fails on `git push`, check this first.

**Non-blocking item to revisit.** The workflow currently emits a Node.js 20 deprecation warning from `actions/checkout@v4` and `actions/setup-python@v5`. GitHub will force these to Node 24 starting **June 2, 2026** and remove Node 20 entirely on **September 16, 2026**. No action needed now — those actions will ship Node-24-compatible versions before the cutoff. If a run starts failing post-June 2026, bump the action versions or add `env: FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`.

**Site dispatch.** After a successful push, `sync.yml` fires a `repository_dispatch` event (type `warrant-sync-completed`) at the sibling repo [`ArlingtonAnnotatedWarrant-Site`](https://github.com/danielpunkass/ArlingtonAnnotatedWarrant-Site), which rebuilds <https://tm.jalkut.com/> with MkDocs Material. The dispatch step uses the `SITE_DISPATCH_TOKEN` secret (a PAT scoped to the site repo with `Contents: read, Metadata: read, Actions: write`, or a classic PAT with `repo` scope). If the secret is unset, the dispatch is skipped with a log message rather than failing the sync — this lets the sync continue even if the PAT is rotated or temporarily revoked.

## Conventions

- **Don't change the matching/parsing logic casually.** The HTML regex parsing is fragile by nature; if a sync run starts producing wrong data, prefer adding a targeted regex tweak over rewriting with a parser library — the script intentionally has no third-party dependencies so it can run in a clean Python install.
- **`source.html` is gitignored.** It's a 1.4MB raw page snapshot useful for local diffing/debugging only. `index.json` carries the structured truth. Don't commit `source.html` even if asked casually — re-confirm intent first, since committing it bloats the repo on every change.
- **The sync bot's commits are the norm on `main`.** Human commits and bot commits coexist; if you push a manual change, expect the bot to commit on top of you within 30 minutes. There's no protected-branch setup, so just `git pull --rebase` if you race it.
