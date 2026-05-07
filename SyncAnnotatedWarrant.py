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
import zlib

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


_PDF_URI_LITERAL_RE = re.compile(rb"/URI\s*\((?P<s>(?:\\.|[^)\\])*)\)", re.DOTALL)
_PDF_URI_HEX_RE = re.compile(rb"/URI\s*<(?P<h>[0-9a-fA-F\s]+)>")
_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def _decode_pdf_literal(b):
    out = bytearray()
    i, n = 0, len(b)
    simple = {ord("n"): 0x0A, ord("r"): 0x0D, ord("t"): 0x09,
              ord("b"): 0x08, ord("f"): 0x0C}
    while i < n:
        c = b[i]
        if c == 0x5C and i + 1 < n:  # backslash
            nxt = b[i + 1]
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if nxt in (0x28, 0x29, 0x5C):  # ( ) \
                out.append(nxt); i += 2; continue
            if 0x30 <= nxt <= 0x37:  # octal escape, 1-3 digits
                j = i + 1
                while j < n and j - i - 1 < 3 and 0x30 <= b[j] <= 0x37:
                    j += 1
                out.append(int(bytes(b[i + 1:j]), 8) & 0xFF)
                i = j; continue
            if nxt in (0x0A, 0x0D):  # line continuation
                i += 2; continue
            i += 1; continue
        out.append(c); i += 1
    return bytes(out)


def extract_pdf_uris(pdf_path):
    """Return ordered, de-duplicated /URI link annotations from a PDF.

    Walks raw bytes plus zlib-decompressed stream contents, looking for /URI
    literal and hex strings. Catches the common case (link annotations in
    Flate-compressed object streams, PDF 1.5+) without pulling in a PDF
    library — the repo's "stdlib only" convention. Encrypted PDFs and the
    rare non-Flate-compressed object streams are silently skipped.
    """
    try:
        with open(pdf_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []

    haystack = bytearray(data)
    for m in _PDF_STREAM_RE.finditer(data):
        try:
            haystack += b"\n" + zlib.decompress(m.group(1))
        except zlib.error:
            pass
    blob = bytes(haystack)

    seen = set()
    uris = []

    def add(s):
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            uris.append(s)

    for m in _PDF_URI_LITERAL_RE.finditer(blob):
        decoded = _decode_pdf_literal(m.group("s"))
        try:
            add(decoded.decode("utf-8"))
        except UnicodeDecodeError:
            add(decoded.decode("latin-1", "replace"))

    for m in _PDF_URI_HEX_RE.finditer(blob):
        hx = bytes(c for c in m.group("h") if c not in b" \t\r\n")
        if len(hx) % 2:
            hx += b"0"
        try:
            decoded = bytes.fromhex(hx.decode())
        except ValueError:
            continue
        try:
            add(decoded.decode("utf-8"))
        except UnicodeDecodeError:
            add(decoded.decode("latin-1", "replace"))

    return [u for u in uris if u.lower().startswith(("http://", "https://", "mailto:"))]


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


def slugify_attachment(att):
    """Map an attachment to (page slug, sidebar/page display name).

    The standard "View full text of Article" PDF gets a fixed short slug and
    the friendlier label "Full Text"; other attachments use a sanitized
    version of their title.
    """
    title = (att.get("title") or "").strip()
    if title.lower() == "view full text of article":
        return "full-text", "Full Text"
    slug = re.sub(r"\s+", "-", title)
    slug = re.sub(r"[^A-Za-z0-9._-]", "", slug)
    slug = slug.strip("-_.").lower() or "attachment"
    return slug, title or "Attachment"


def write_article_summary(article_dir, article, att_pages):
    """Write the article's index.md (Summary page: description, sponsor, links).

    `att_pages` is the same list `attachment_pages_for` produced — used to
    mirror the sidebar attachment list inline as a "Resources" section so
    they're discoverable from the summary view.
    """
    lines = [f"# Article {article['articleNumber']}: {article['title']}", ""]
    if article["sponsor"]:
        lines += [f"**Sponsor:** {article['sponsor']}", ""]
    if article["description"]:
        lines += ["## Description", "", article["description"], ""]
    if att_pages:
        lines += ["## Resources", ""]
        for slug, name, _ in att_pages:
            lines.append(f"- [{name}]({slug}.md)")
        lines.append("")
    if article["externalLinks"]:
        lines += ["## External Links", ""]
        for link in article["externalLinks"]:
            lines.append(f"- [{link['label']}]({link['url']})")
        lines.append("")
    lines += [
        "---",
        f"*Source item id:* `{article['itemId']}`  ",
        f"*Source:* <{SOURCE_URL}>",
        "",
    ]
    with open(os.path.join(article_dir, "index.md"), "w") as fh:
        fh.write("\n".join(lines))


def write_attachment_page(article_dir, slug, display_name, att):
    """Write a per-attachment page that embeds the PDF in an iframe.

    Requires the `md_in_html` and `attr_list` markdown extensions on the
    site side (the iframe and `{target="_blank"}` annotation otherwise pass
    through as raw text).
    """
    filename = att.get("filename")
    if not filename:
        return
    encoded = urllib.parse.quote(filename)
    is_pdf = filename.lower().endswith(".pdf")

    # Path scheme below is intentionally asymmetric:
    #   - The <iframe src> is raw HTML; MkDocs doesn't rewrite it, so it must
    #     match the *served* URL. With use_directory_urls (default), this page
    #     is at .../<slug>/, and the PDF lives one level up — hence `../`.
    #   - The markdown [link](...) IS rewritten by MkDocs's link resolver,
    #     which evaluates relative paths against the *source* location
    #     (articles/Article-N/<slug>.md). The bare filename resolves to the
    #     PDF sitting alongside the source, and MkDocs rewrites the href to
    #     the correct output URL — and validates it during build.
    lines = [f"# {display_name}", ""]
    if is_pdf:
        title_attr = html.escape(display_name, quote=True)
        lines += [
            f'<iframe src="../{encoded}" style="width:100%; height:80vh; border:0;" '
            f'title="{title_attr}"></iframe>',
            "",
            f'[Open PDF in new tab]({encoded}){{target="_blank" rel="noopener"}}',
            "",
        ]
    else:
        lines += [f"[Download attachment]({encoded})", ""]
    pdf_links = att.get("pdfLinks") or []
    if pdf_links:
        lines += ["## Links in this PDF", ""]
        for url in pdf_links:
            lines.append(f"- <{url}>")
        lines.append("")
    with open(os.path.join(article_dir, f"{slug}.md"), "w") as fh:
        fh.write("\n".join(lines))


def write_pages_file(article_dir, article, nav_entries):
    """Write a `.pages` file for the awesome-pages MkDocs plugin.

    Sets the section title to the full article heading (so the sidebar shows
    "Article 2: STATE OF THE TOWN ADDRESS" instead of just "Article-02") and
    declares the order of child pages: Summary, then attachments.
    """
    section_title = f"Article {article['articleNumber']}: {article['title']}"
    lines = [
        f"title: {json.dumps(section_title)}",
        "nav:",
        "  - Summary: index.md",
    ]
    for slug, name in nav_entries:
        lines.append(f"  - {json.dumps(name)}: {slug}.md")
    with open(os.path.join(article_dir, ".pages"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def attachment_pages_for(article):
    """Return [(slug, display_name, att), ...] in sidebar order.

    Full Text always comes first when present; remaining attachments keep the
    order they appear in the warrant. Slug collisions within an article get
    `-2`, `-3`, ... suffixes.
    """
    pages = []
    used = set()
    for att in article["attachments"]:
        if not att.get("filename"):
            continue
        slug, name = slugify_attachment(att)
        base = slug
        n = 2
        while slug in used:
            slug = f"{base}-{n}"
            n += 1
        used.add(slug)
        pages.append((slug, name, att))
    ft_idx = next((i for i, p in enumerate(pages) if p[0] == "full-text"), None)
    if ft_idx is not None and ft_idx != 0:
        pages.insert(0, pages.pop(ft_idx))
    return pages


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
        dir_link = f"./{ARTICLES_SUBDIR}/{article_dirname(a)}/index.md"
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

            if old and old.get("filename") == target_filename and "pdfLinks" in old:
                att["pdfLinks"] = list(old["pdfLinks"])
            elif target_filename.lower().endswith(".pdf"):
                att["pdfLinks"] = extract_pdf_uris(os.path.join(adir, target_filename))
            else:
                att["pdfLinks"] = []

        # Generate the per-article page set: index.md (Summary), one wrapper
        # page per attachment (which embeds the PDF in an iframe), and a
        # .pages file declaring sidebar title + child order.
        att_pages = attachment_pages_for(article)
        write_article_summary(adir, article, att_pages)
        for slug, name, att in att_pages:
            write_attachment_page(adir, slug, name, att)
        write_pages_file(adir, article, [(s, n) for s, n, _ in att_pages])

        # Remove anything we didn't just generate or download.
        preserved = set(kept_filenames)
        preserved.update({"index.md", ".pages"})
        preserved.update(f"{s}.md" for s, _, _ in att_pages)
        for entry in os.listdir(adir):
            if entry.startswith(".pending-"):
                os.remove(os.path.join(adir, entry))
                continue
            if entry not in preserved:
                os.remove(os.path.join(adir, entry))
                print(f"  Article {article['articleNumber']:>2}: removed stale {entry}")

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
