#!/usr/bin/env python3
"""
Sync the Arlington Town Meeting Annotated Warrant from primegov.com.

Vendored copy for the ArlingtonAnnotatedWarrant mirror repo. The canonical
copy lives in the parent TMM project; the only difference is ARCHIVE_DIR,
which here points at the repo root so the script writes alongside itself.

The Annotated Warrant is a living document: articles, descriptions, sponsors,
and supporting attachments evolve as Town Meeting approaches and proceeds.
This script is idempotent — re-run it anytime to pull the latest state.

Each attachment on primegov is identified by an immutable historyId. We track
the historyId per-attachment in index.json; on re-sync we only download
attachments whose historyId has changed (or which are new). Files for
attachments that no longer exist upstream are removed.
"""

import datetime
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

MEETING_TEMPLATE_ID = 1659
SOURCE_URL = f"https://arlingtonma.primegov.com/Portal/Meeting?meetingTemplateId={MEETING_TEMPLATE_ID}"
ATTACHMENT_URL = "https://arlingtonma.primegov.com/api/compilemeetingattachmenthistory/historyattachment/?historyId={historyId}"

ARCHIVE_DIR = "."
ARTICLES_SUBDIR = "articles"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "TMM-Warrant-Sync/1.0"})
    with urllib.request.urlopen(req) as resp:
        return resp.read(), dict(resp.headers)


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def safe_filename(name):
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name.strip().strip(".") or "untitled"


def parse_articles(html_text):
    """Yield one dict per article block in the page."""
    chunks = re.split(
        r'(?=<table[^>]*class="item-table-fromdocx"[^>]*data-itemid=")',
        html_text,
    )
    articles = []
    for chunk in chunks:
        m = re.match(
            r'<table[^>]*class="item-table-fromdocx"[^>]*data-itemid="(\d+)"',
            chunk,
        )
        if not m:
            continue
        item_id = int(m.group(1))

        title_match = re.search(
            r'<span[^>]*font-weight:bold[^>]*>\s*Article\s*#\s*(\d+)\s+([^<]+?)\s*</span>',
            chunk,
        )
        article_number = None
        title = None
        if title_match:
            article_number = int(title_match.group(1))
            title = strip_tags(title_match.group(2))

        body_match = re.search(
            r'data-createdfromdocx="True"[^>]*>(.*?)<div style="display:none;" class="item_contents"',
            chunk,
            re.DOTALL,
        )
        body_html = body_match.group(1) if body_match else ""

        paragraphs = []
        for p in re.findall(r"<p[^>]*>(.*?)</p>", body_html, re.DOTALL):
            text = strip_tags(p)
            if text:
                paragraphs.append(text)

        meaningful = [p for p in paragraphs if not p.startswith("Article #")]

        sponsor = None
        description_paragraphs = []
        for p in meaningful:
            if p.startswith("Inserted at the request of"):
                sponsor = p
            else:
                description_paragraphs.append(p)
        description = "\n\n".join(description_paragraphs).strip()

        external_links = []
        for href, label in re.findall(
            r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            body_html,
            re.DOTALL,
        ):
            label_clean = strip_tags(label)
            if label_clean and "primegov.com" not in href:
                external_links.append({"url": href, "label": label_clean})

        attachments = []
        attach_section = re.search(
            r'<div style="display:none;" class="item_contents"[^>]*>(.*?)(?:</td>|<td class="optionalButtonsCell")',
            chunk,
            re.DOTALL,
        )
        if attach_section:
            for holder in re.findall(
                r'<div class="attachment-holder[^"]*">(.*?)</div>',
                attach_section.group(1),
                re.DOTALL,
            ):
                dl = re.search(
                    r'<a\s+href="/api/compilemeetingattachmenthistory/historyattachment/\?historyId=([0-9a-fA-F-]+)"[^>]*title="Download\s+([^"]+)"',
                    holder,
                )
                if not dl:
                    dl = re.search(
                        r'<a\s+href="/api/compilemeetingattachmenthistory/historyattachment/\?historyId=([0-9a-fA-F-]+)"[^>]*?title="Download\s+([^"]+)"',
                        holder,
                    )
                if dl:
                    attachments.append({
                        "historyId": dl.group(1).strip(),
                        "title": html.unescape(dl.group(2)).strip(),
                    })

        articles.append({
            "itemId": item_id,
            "articleNumber": article_number,
            "title": title or "",
            "sponsor": sponsor,
            "description": description,
            "externalLinks": external_links,
            "attachments": attachments,
        })

    articles.sort(key=lambda a: (a["articleNumber"] is None, a["articleNumber"] or 0))
    return articles


def download_attachment(history_id, dest_path):
    url = ATTACHMENT_URL.format(historyId=history_id)
    req = urllib.request.Request(url, headers={"User-Agent": "TMM-Warrant-Sync/1.0"})
    with urllib.request.urlopen(req) as resp:
        disposition = resp.headers.get("Content-Disposition", "")
        body = resp.read()
        fname_match = re.search(r'filename="([^"]+)"', disposition)
        server_filename = fname_match.group(1) if fname_match else None
        with open(dest_path, "wb") as fh:
            fh.write(body)
        return len(body), server_filename


def article_dirname(article):
    return f"Article-{article['articleNumber']:02d}" if article["articleNumber"] else f"Item-{article['itemId']}"


def write_article_md(article_dir, article):
    lines = [f"# Article {article['articleNumber']}: {article['title']}", ""]
    if article["sponsor"]:
        lines += [f"**Sponsor:** {article['sponsor']}", ""]
    if article["description"]:
        lines += ["## Description", "", article["description"], ""]
    if article["externalLinks"]:
        lines += ["## External Links", ""]
        for link in article["externalLinks"]:
            lines.append(f"- [{link['label']}]({link['url']})")
        lines.append("")
    if article["attachments"]:
        lines += ["## Attachments", ""]
        for att in article["attachments"]:
            local = att.get("filename")
            if local:
                lines.append(f"- [{att['title']}](./{urllib.parse.quote(local)})")
            else:
                lines.append(f"- {att['title']} (not yet downloaded)")
        lines.append("")
    lines += [
        "---",
        f"*Source item id:* `{article['itemId']}`  ",
        f"*Source:* <{SOURCE_URL}>",
        "",
    ]
    with open(os.path.join(article_dir, "article.md"), "w") as fh:
        fh.write("\n".join(lines))


def write_index_md(archive_dir, articles, synced_at):
    lines = [
        "# Annotated Warrant — Index",
        "",
        f"Source: <{SOURCE_URL}>  ",
        f"Last synced: {synced_at}",
        "",
        f"{len(articles)} articles. Re-run `python3 SyncAnnotatedWarrant.py` from the project root to refresh.",
        "",
        "| # | Title | Sponsor | Attachments |",
        "| ---: | --- | --- | ---: |",
    ]
    for a in articles:
        sponsor = (a["sponsor"] or "").replace("Inserted at the request of ", "")
        title = a["title"].replace("|", "\\|")
        sponsor = sponsor.replace("|", "\\|")
        dir_link = f"./{ARTICLES_SUBDIR}/{article_dirname(a)}/article.md"
        lines.append(
            f"| {a['articleNumber']} | [{title}]({urllib.parse.quote(dir_link, safe='/.:')}) | {sponsor} | {len(a['attachments'])} |"
        )
    lines.append("")
    with open(os.path.join(archive_dir, "INDEX.md"), "w") as fh:
        fh.write("\n".join(lines))


def write_readme(archive_dir):
    readme = """# Annotated Warrant Archive

Mirror of the Arlington Town Meeting Annotated Warrant from primegov.com.

The Annotated Warrant supplements the high-level ATM Warrant PDF with the
actual vote language, supporting documents, and committee voting
recommendations. It is a *living* document — new attachments are added and
existing ones revised as Town Meeting approaches and proceeds.

## Layout

- `INDEX.md` — Human-readable index of all articles.
- `index.json` — Machine-readable manifest. Source of truth for what has been
  downloaded, including each attachment's `historyId` (primegov's stable
  identifier).
- `source.html` — Snapshot of the last-fetched warrant page (kept for
  diffing/debugging).
- `articles/Article-NN/` — One directory per article, containing
  `article.md` and any downloaded attachment files.

## Updating

From the project root:

```bash
python3 SyncAnnotatedWarrant.py
```

The sync is incremental: attachments whose `historyId` has not changed are
skipped. Attachments that no longer exist upstream are removed. Article
metadata (titles, descriptions, sponsors, external links) is regenerated on
every run.

## Source

Index page: <https://arlingtonma.primegov.com/Portal/Meeting?meetingTemplateId=1659>
"""
    with open(os.path.join(archive_dir, "README.md"), "w") as fh:
        fh.write(readme)


def sync():
    project_root = os.path.dirname(os.path.abspath(__file__))
    archive_dir = os.path.join(project_root, ARCHIVE_DIR)
    articles_dir = os.path.join(archive_dir, ARTICLES_SUBDIR)
    os.makedirs(articles_dir, exist_ok=True)

    print(f"Fetching {SOURCE_URL}")
    page_bytes, _ = fetch(SOURCE_URL)
    page_text = page_bytes.decode("utf-8", errors="replace")
    with open(os.path.join(archive_dir, "source.html"), "w") as fh:
        fh.write(page_text)

    articles = parse_articles(page_text)
    print(f"Parsed {len(articles)} articles")

    manifest_path = os.path.join(archive_dir, "index.json")
    existing = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as fh:
                old = json.load(fh)
            for a in old.get("articles", []):
                existing[a["itemId"]] = a
        except (OSError, json.JSONDecodeError):
            pass

    seen_dirs = set()
    for article in articles:
        adir = os.path.join(articles_dir, article_dirname(article))
        os.makedirs(adir, exist_ok=True)
        seen_dirs.add(os.path.basename(adir))

        old_atts = {a["historyId"]: a for a in existing.get(article["itemId"], {}).get("attachments", [])}
        kept_filenames = set()

        for att in article["attachments"]:
            old = old_atts.get(att["historyId"])
            target_filename = None
            if old and old.get("filename"):
                candidate = os.path.join(adir, old["filename"])
                if os.path.exists(candidate):
                    target_filename = old["filename"]
                    att["filename"] = target_filename
                    att["size"] = old.get("size")
            if not target_filename:
                tmp_path = os.path.join(adir, f".pending-{att['historyId']}")
                size, server_name = download_attachment(att["historyId"], tmp_path)
                final_name = safe_filename(server_name or f"{att['title']}.pdf")
                final_path = os.path.join(adir, final_name)
                if os.path.exists(final_path):
                    os.remove(final_path)
                os.rename(tmp_path, final_path)
                att["filename"] = final_name
                att["size"] = size
                target_filename = final_name
                print(f"  Article {article['articleNumber']:>2}: downloaded {final_name} ({size} bytes)")
            kept_filenames.add(target_filename)

        for entry in os.listdir(adir):
            if entry == "article.md":
                continue
            if entry.startswith(".pending-"):
                os.remove(os.path.join(adir, entry))
                continue
            if entry not in kept_filenames:
                os.remove(os.path.join(adir, entry))
                print(f"  Article {article['articleNumber']:>2}: removed stale {entry}")

        write_article_md(adir, article)

    for entry in os.listdir(articles_dir):
        full = os.path.join(articles_dir, entry)
        if os.path.isdir(full) and entry not in seen_dirs:
            for f in os.listdir(full):
                os.remove(os.path.join(full, f))
            os.rmdir(full)
            print(f"Removed orphan article dir: {entry}")

    synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "source": SOURCE_URL,
        "meetingTemplateId": MEETING_TEMPLATE_ID,
        "lastSynced": synced_at,
        "articleCount": len(articles),
        "articles": articles,
    }

    # If nothing but the timestamp would change, reuse the prior lastSynced so
    # this run produces a no-op diff. Otherwise the scheduled CI job would
    # commit every 30 minutes even when upstream is static.
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as fh:
                prior = json.load(fh)
            prior_ts = prior.get("lastSynced")
            if prior_ts and {k: v for k, v in prior.items() if k != "lastSynced"} == \
                           {k: v for k, v in manifest.items() if k != "lastSynced"}:
                manifest["lastSynced"] = prior_ts
                synced_at = prior_ts
        except (OSError, json.JSONDecodeError):
            pass

    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    write_index_md(archive_dir, articles, synced_at)
    write_readme(archive_dir)
    print(f"\nWrote manifest to {manifest_path}")
    print(f"Archive root: {archive_dir}")


if __name__ == "__main__":
    sync()
