# Annotated Warrant Archive

Mirror of the Arlington Town Meeting Annotated Warrant from primegov.com.

The Annotated Warrant supplements the high-level ATM Warrant PDF with the
actual vote language, supporting documents, and committee voting
recommendations. It is a *living* document — new attachments are added and
existing ones revised as Town Meeting approaches and proceeds.

## Layout

- `INDEX.md` — Human-readable index of all articles.
- `index.json` — Machine-readable manifest. Source of truth for what has been
  downloaded, including each attachment's `historyId` (primegov's stable
  identifier) and the cumulative `changeLog` of sync events.
- `source.html` — Snapshot of the last-fetched warrant page (kept for
  diffing/debugging; gitignored).
- `articles/Article-NN/` — One directory per article, containing the article
  summary `index.md`, any downloaded attachment files, and per-attachment
  subdirectories that hold the rendered attachment page.
- `articles/recent-updates/` — One markdown page per sync run that produced
  changes, summarising the events.

## Updating

```bash
python3 SyncAnnotatedWarrant.py
```

Run from this directory — the script writes to its own working tree
(`ARCHIVE_DIR = "."`).

The sync is incremental: attachments whose `historyId` has not changed are
skipped. Attachments that no longer exist upstream are removed. Article
metadata (titles, descriptions, requesters, external links) is regenerated on
every run.

## Source

Index page: <https://arlingtonma.primegov.com/Portal/Meeting?meetingTemplateId=1659>
