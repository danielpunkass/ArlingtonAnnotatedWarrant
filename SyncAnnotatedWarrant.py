#!/usr/bin/env python3
"""
Sync the Arlington Town Meeting Annotated Warrant from primegov.com.

Vendored copy for the ArlingtonAnnotatedWarrant mirror repo. The canonical
copy lives in the parent TMM project; the only difference is ARCHIVE_DIR,
which here points at the repo root so the script writes alongside itself.

The Annotated Warrant is a living document: articles, descriptions, requesters,
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
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zlib
from zoneinfo import ZoneInfo

MEETING_TEMPLATE_ID = 1659
SOURCE_URL = f"https://arlingtonma.primegov.com/Portal/Meeting?meetingTemplateId={MEETING_TEMPLATE_ID}"
ATTACHMENT_URL = "https://arlingtonma.primegov.com/api/compilemeetingattachmenthistory/historyattachment/?historyId={historyId}"

# The Moderator's live progress tracker (published HTML view of a Google
# Sheet). Used to classify each article as "disposed" or "pending" so that
# disposed articles can be tucked under a collapsed sidebar group, leaving
# only the still-to-be-debated articles cluttering the main nav.
PROGRESS_PUBHTML_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSKCsU9snM1MNvj0X7eOc6EErCssVC8Z0fOGvBuIoFMvj-6CUXfiReMZygMgt5MHmtVJUwkntpvQmxf"
    "/pubhtml/sheet?headers=false&gid=632365380"
)

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


STATUS_LABELS = {
    "y": "Passed",
    "n": "Failed",
    "w": "Withdrawn",
    "t": "Tabled",
    "p": "Postponed",
    "n/a": "No Action",
    "r/c": "Referred to Committee",
}

# Status codes that finalize the article at this Town Meeting. Tabled
# and postponed articles get a status code and a date too, but they are
# still open — Town Meeting will return to them — so they stay in the
# pending sidebar group. The provisional disposition note ("Tabled on
# April 27, 2026.") is still shown on the article's summary page either
# way, since that's the most recent factual outcome.
TERMINAL_STATUS_CODES = {"y", "n", "w", "n/a", "r/c"}

# Admonition flavor (Material for MkDocs styles each with its own
# accent color and icon) per status code. The grouping is rough but
# legible at a glance:
#   success (green) — article passed
#   failure (red)   — article failed
#   warning (amber) — provisional outcome the meeting will revisit
#   info (blue)     — article handed off to a committee for follow-up
#   note (gray)     — neutral terminal outcomes (withdrawn, no action)
ADMONITION_TYPE_BY_CODE = {
    "y": "success",
    "n": "failure",
    "w": "note",
    "t": "warning",
    "p": "warning",
    "n/a": "note",
    "r/c": "info",
}

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def humanize_date(value, year=None):
    """`4/27` → `April 27, 2026`. Year defaults to the current calendar
    year (Town Meeting doesn't span years, so the sync year is correct).
    Returns None for empty/`-`, raw input on unrecognized format."""
    if not value or value == "-":
        return None
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})\s*$", value)
    if not m:
        return value
    month_idx = int(m.group(1)) - 1
    if not 0 <= month_idx < 12:
        return value
    if year is None:
        year = datetime.datetime.now().year
    return f"{_MONTHS[month_idx]} {int(m.group(2))}, {year}"


def _vote_int(s):
    """Parse a vote-count cell. Returns None for empty (voice/acclamation)."""
    s = (s or "").strip()
    if not s.isdigit():
        return None
    return int(s)


def fetch_article_progress():
    """Return {articleNumber: {"status": "disposed"|"pending", "code": ..., "label": ...,
    "date": ..., "yesVotes": ..., "noVotes": ...}} from the Moderator's live tracker.

    The sheet's columns (post the article-number/title pair) are: required
    vote threshold, status code, disposition date, a cross-reference column,
    yes votes, no votes, total, abstain, percentage, scheduled date — the
    fields we surface are status / date / yes / no.

    Network or parse failures return an empty dict so the build continues
    even when the sheet is unreachable; missing entries default to pending.
    """
    try:
        body, _ = fetch(PROGRESS_PUBHTML_URL)
        text = body.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — network/IO failure is non-fatal
        print(f"WARNING: could not fetch progress sheet: {exc}", file=sys.stderr)
        return {}

    progress = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.DOTALL):
        cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
        if len(cells) < 4:
            continue
        num_str = cells[0]
        if not num_str.isdigit():
            continue
        n = int(num_str)
        code = cells[3].strip()
        has_outcome = bool(code and code != "-")
        # `status` drives sidebar grouping: only terminal codes get
        # tucked under "Disposed Articles". Tabled/postponed articles
        # carry a `disposition` (so the summary page can show "Tabled
        # on April 27, 2026.") but stay in the pending group.
        is_terminal = has_outcome and code.lower() in TERMINAL_STATUS_CODES
        entry = {"status": "disposed" if is_terminal else "pending"}
        if has_outcome:
            entry["disposition"] = {
                "code": code,
                "label": STATUS_LABELS.get(code.lower(), code),
                "date": humanize_date(cells[4]) if len(cells) > 4 else None,
                "yesVotes": _vote_int(cells[6]) if len(cells) > 6 else None,
                "noVotes": _vote_int(cells[7]) if len(cells) > 7 else None,
            }
        progress[n] = entry
    return progress


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


_PDF2HTMLEX_DOCKER_TAG = (
    "pdf2htmlex/pdf2htmlex:0.18.8.rc2-master-20200820-ubuntu-20.04-x86_64"
)
_PDF2HTMLEX_FLAGS = [
    "--zoom", "1.5",
    "--embed-css", "1",
    "--embed-font", "1",
    "--embed-image", "1",
    "--embed-javascript", "0",
    "--embed-outline", "0",
    "--process-outline", "0",
    "--printing", "0",
]


def convert_pdf_to_html(pdf_path, html_path):
    """Convert `pdf_path` to a self-contained HTML file at `html_path`.

    Tries a local `pdf2htmlEX` binary first, then falls back to its
    official Docker image. Returns True on success, False if neither
    path produced output — callers should degrade to the iframe view
    rather than fail the sync.

    pdf2htmlEX writes its output into a `--dest-dir`, so we always
    point that at the article directory and pass bare basenames; this
    avoids host-vs-container path translation when running under
    Docker.
    """
    in_dir = os.path.dirname(os.path.abspath(pdf_path))
    in_name = os.path.basename(pdf_path)
    out_name = os.path.basename(html_path)
    args = _PDF2HTMLEX_FLAGS + ["--dest-dir", "/data", in_name, out_name]

    if shutil.which("pdf2htmlEX"):
        try:
            r = subprocess.run(
                ["pdf2htmlEX"] + _PDF2HTMLEX_FLAGS + ["--dest-dir", in_dir, in_name, out_name],
                cwd=in_dir,
                capture_output=True,
                timeout=180,
            )
            if r.returncode == 0 and os.path.exists(html_path):
                return True
        except subprocess.TimeoutExpired:
            pass

    if shutil.which("docker"):
        try:
            r = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{in_dir}:/data",
                    "-w", "/data",
                    _PDF2HTMLEX_DOCKER_TAG,
                ] + args,
                capture_output=True,
                timeout=300,
            )
            if r.returncode == 0 and os.path.exists(html_path):
                return True
        except subprocess.TimeoutExpired:
            pass

    return False


_HTML_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_HTML_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.DOTALL | re.IGNORECASE)
_HTML_A_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"', re.IGNORECASE)
_HTML_TEXT_DIV_RE = re.compile(r'<div class="t [^"]*"[^>]*>(.*?)</div>', re.DOTALL)


def extract_html_links(html_path):
    """Return [(label, url), ...] from a pdf2htmlEX-generated HTML.

    pdf2htmlEX renders each /URI annotation as an invisible <a> over an
    empty positional <div>; the visible link text lives in a sibling
    <div class="t ...">…</div> immediately preceding the <a>. We use that
    DOM adjacency as a heuristic to recover the original anchor label
    (e.g. "Finance Committee Report") rather than displaying the raw URL.
    Falls back to the URL when no preceding text div is found.

    Returns ordered, de-duplicated by URL. Filters to http(s)/mailto.
    """
    try:
        with open(html_path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return []
    body_match = _HTML_BODY_RE.search(content)
    if not body_match:
        return []
    body = body_match.group(1)

    seen = set()
    out = []
    for m in _HTML_A_RE.finditer(body):
        href = html.unescape(m.group(1))
        if not href.lower().startswith(("http://", "https://", "mailto:")):
            continue
        if href in seen:
            continue
        seen.add(href)

        before = body[:m.start()]
        text_matches = list(_HTML_TEXT_DIV_RE.finditer(before))
        label = href
        if text_matches:
            txt = re.sub(r"<[^>]+>", "", text_matches[-1].group(1))
            txt = html.unescape(txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt:
                label = txt
        out.append((label, href))
    return out


def extract_html_body(html_path):
    """Read a pdf2htmlEX HTML file and return (style_block, body_inner).

    pdf2htmlEX produces a self-contained document with absolutely-positioned
    page divs and font/CSS embedded in <head>. We pull both out as raw
    strings so the attachment markdown page can splice them in directly:
    the page is part of the host document (Cmd+F-able, indexed by the
    site's search), not iframed.

    Returns (None, None) on read or parse failure.
    """
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return (None, None)
    styles = "\n".join(_HTML_STYLE_RE.findall(content))
    body_match = _HTML_BODY_RE.search(content)
    if not body_match:
        return (None, None)
    return (styles, body_match.group(1))


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

        requester = None
        description_paragraphs = []
        for p in meaningful:
            if p.startswith("Inserted at the request of"):
                requester = p
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
            "requester": requester,
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
    """Write the article's index.md (Summary page: description, requester, links).

    `att_pages` is the same list `attachment_pages_for` produced — used to
    mirror the sidebar attachment list inline as a "Resources" section so
    they're discoverable from the summary view.
    """
    lines = [f"# Article {article['articleNumber']}: {article['title']}", ""]
    disp = article.get("disposition")
    if disp:
        # Render the outcome as a title-only admonition under the
        # heading: "!!! success \"Passed on April 27, 2026\"". The
        # admonition flavor (success/failure/warning/...) color-codes
        # the outcome at a glance. Vote tallies are kept in index.json
        # for downstream consumers but not rendered yet.
        label = disp.get("label") or "Disposed"
        date = disp.get("date")
        sentence = f"{label} on {date}" if date else label
        adm_type = ADMONITION_TYPE_BY_CODE.get(
            (disp.get("code") or "").lower(), "note"
        )
        lines += [f'!!! {adm_type} "{sentence}"', ""]
    if article["requester"]:
        lines += [f"_{article['requester']}_", ""]
    if article["description"]:
        lines += ["## Description", "", article["description"], ""]
    if att_pages:
        lines += ["## Resources", ""]
        for slug, name, _ in att_pages:
            lines.append(f"- [{name}]({slug}/index.md)")
        lines.append("")
    if article["externalLinks"]:
        lines += ["## External Links", ""]
        for link in article["externalLinks"]:
            lines.append(f"- [{link['label']}]({link['url']})")
        lines.append("")
    lines += [
        "---",
        f"*Source:* <{SOURCE_URL}>",
        "",
    ]
    with open(os.path.join(article_dir, "index.md"), "w") as fh:
        fh.write("\n".join(lines))


def link_label(url):
    """Compact sidebar label for an external URL: drop scheme, truncate."""
    cleaned = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).rstrip("/")
    return cleaned if len(cleaned) <= 48 else cleaned[:47] + "…"


def write_attachment_page(article_dir, slug, display_name, att):
    """Write a per-attachment subdirectory: index.md (iframe + searchable
    text) plus a .pages file declaring the section title and any /URI
    links found in the PDF as nav children (rendered with a link icon by
    extra.css). Each attachment thus becomes a navigable section.

    Requires the `md_in_html` and `attr_list` markdown extensions on the
    site side (the iframe and `{target="_blank"}` annotation otherwise
    pass through as raw text).
    """
    filename = att.get("filename")
    if not filename:
        return
    encoded = urllib.parse.quote(filename)
    is_pdf = filename.lower().endswith(".pdf")

    sub_dir = os.path.join(article_dir, slug)
    os.makedirs(sub_dir, exist_ok=True)

    # The PDF lives one directory up from this attachment's index.md, so
    # both the served-URL iframe src and the source-relative markdown link
    # use `../<filename>`. (MkDocs's link checker validates the markdown
    # link against source paths; the iframe is raw HTML and not checked.)
    lines = [f"# {display_name}", ""]
    if is_pdf:
        lines += [
            f'[Open PDF in new window](../{encoded}){{target="_blank" rel="noopener"}}',
            "",
        ]
        # Prefer the pdf2htmlEX render (real DOM, Cmd+F-able, indexed by
        # site search). Iframe is a fallback for the case where conversion
        # didn't happen (no pdf2htmlEX, no Docker, or a tool error).
        html_filename = att.get("htmlFilename")
        styles = body = None
        if html_filename:
            styles, body = extract_html_body(
                os.path.join(article_dir, html_filename)
            )
        if body:
            if styles:
                lines += [styles, ""]
            lines += ['<div class="pdf-rendered">', body, "</div>", ""]
        else:
            title_attr = html.escape(display_name, quote=True)
            lines += [
                f'<iframe src="../{encoded}" style="width:100%; height:80vh; border:0;" '
                f'title="{title_attr}"></iframe>',
                "",
            ]
    else:
        lines += [f"[Download attachment](../{encoded})", ""]

    with open(os.path.join(sub_dir, "index.md"), "w") as fh:
        fh.write("\n".join(lines))

    # .pages: section title + index.md as the section's overview page,
    # followed by each /URI link as a nav child. The empty list-of-links
    # case still needs a .pages so the section title carries through.
    pdf_links = att.get("pdfLinks") or []
    pages_lines = [
        f"title: {json.dumps(display_name)}",
        "nav:",
        "  - index.md",
    ]
    for link in pdf_links:
        # New dict shape carries the anchor text; legacy string entries
        # have no label, so we synthesize one from the URL.
        if isinstance(link, dict):
            url = link.get("url", "")
            label = link.get("label") or link_label(url)
        else:
            url = link
            label = link_label(url)
        pages_lines.append(f"  - {json.dumps(label)}: {json.dumps(url)}")
    with open(os.path.join(sub_dir, ".pages"), "w") as fh:
        fh.write("\n".join(pages_lines) + "\n")


def write_pages_file(article_dir, article, nav_entries):
    """Write a `.pages` file for the awesome-pages MkDocs plugin.

    Sets the section title to a short label ("Article 2") so the sidebar
    stays scannable across all 95 articles; the full title still appears
    as the H1 of the article's summary page once selected. Declares the
    child page order (Summary, then attachments).
    """
    if article.get("articleNumber"):
        section_title = f"Article {article['articleNumber']}"
    else:
        section_title = f"Item {article['itemId']}"
    lines = [
        f"title: {json.dumps(section_title)}",
        "nav:",
        "  - Summary: index.md",
    ]
    for slug, name in nav_entries:
        # Reference the attachment subdirectory; awesome-pages reads its
        # own .pages for section title and child link order.
        lines.append(f"  - {json.dumps(name)}: {slug}")
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


def write_root_pages(archive_dir, articles):
    """Write the root `.pages` for awesome-pages.

    Sidebar order:
      Index → "Disposed Articles" group → "Tabled and Postponed
      Articles" group → still-pending articles at the top level. Both
      grouped sections are only emitted when non-empty. Tabled /
      postponed articles are not "disposed" — Town Meeting will return
      to them — but they're not actively pending either, so they get
      their own collapsed group.

    awesome-pages' `... | <glob>` filter only operates at the current
    directory level, so we enumerate the subdirs ourselves.
    """
    disposed = [a for a in articles if a.get("status") == "disposed"]
    deferred = [a for a in articles if a.get("status") == "pending" and a.get("disposition")]
    pending = [a for a in articles if a.get("status") == "pending" and not a.get("disposition")]

    lines = ["nav:", "  - Index: index.md"]
    if disposed:
        lines.append("  - Disposed Articles:")
        for a in disposed:
            lines.append(f"    - {ARTICLES_SUBDIR}/{article_dirname(a)}")
    if deferred:
        lines.append("  - Tabled and Postponed Articles:")
        for a in deferred:
            lines.append(f"    - {ARTICLES_SUBDIR}/{article_dirname(a)}")
    for a in pending:
        lines.append(f"  - {ARTICLES_SUBDIR}/{article_dirname(a)}")
    with open(os.path.join(archive_dir, ".pages"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def write_index_md(archive_dir, articles, synced_at):
    # Convert the manifest's UTC ISO timestamp into a friendly local-time
    # string for the page header. Eastern time is the relevant timezone —
    # Arlington Town Meeting members read this in MA. `%-d` / `%-I` strip
    # leading zeros (GNU strftime); macOS and Linux both honor that.
    try:
        utc_dt = datetime.datetime.fromisoformat(synced_at)
        local_dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
        synced_human = local_dt.strftime("%A, %B %-d at %-I:%M%p")
    except ValueError:
        synced_human = synced_at

    def render_row(a):
        # "Inserted at the request of the Moderator" → "Moderator". The
        # leading "the " is stripped because it's grammatical filler in the
        # full phrase ("at the request of THE Moderator") — once that
        # context is gone, "the Moderator" reads oddly as a column entry.
        requester = (a["requester"] or "").replace("Inserted at the request of ", "")
        requester = re.sub(r"^the\s+", "", requester, flags=re.IGNORECASE)
        title = a["title"].replace("|", "\\|")
        requester = requester.replace("|", "\\|")
        dir_link = f"./{ARTICLES_SUBDIR}/{article_dirname(a)}/index.md"
        return (
            f"| {a['articleNumber']} | [{title}]({urllib.parse.quote(dir_link, safe='/.:')}) "
            f"| {requester} | {len(a['attachments'])} |"
        )

    # The Index is a single overview of every article regardless of
    # state — disposed/tabled/postponed/pending all share one table.
    # The sidebar handles the disposition-based grouping; here we just
    # want the full list in article order.
    lines = [
        "# Annotated Warrant — Index",
        "",
        # Two trailing spaces = markdown line break, so "Source:" and the
        # "Last synced" line render as adjacent lines in one paragraph
        # rather than separated by a paragraph gap.
        f"Source: [Official 2026 Annotated Town Warrant]({SOURCE_URL})  ",
        f"<small>Last synced on {synced_human}</small>.",
        "",
        "| # | Title | Requested By | Attachments |",
        "| ---: | --- | --- | ---: |",
    ]
    for a in articles:
        lines.append(render_row(a))
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
metadata (titles, descriptions, requesters, external links) is regenerated on
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

    # Tag each article as disposed/pending using the Moderator's live
    # progress tracker. Missing entries (or sheet-fetch failure) default
    # to "pending" — better to clutter the sidebar than to hide an
    # article that hasn't actually been disposed of. Disposed articles
    # also get a `disposition` dict (code/label/date/votes) which the
    # per-article summary page surfaces near the title.
    progress = fetch_article_progress()
    for a in articles:
        entry = progress.get(a["articleNumber"], {"status": "pending"})
        a["status"] = entry["status"]
        if entry.get("disposition"):
            a["disposition"] = entry["disposition"]
    disposed_count = sum(1 for a in articles if a["status"] == "disposed")
    print(f"  {disposed_count} disposed, {len(articles) - disposed_count} pending")

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

            # Convert PDF → self-contained HTML for inline rendering. Skip
            # if the PDF wasn't re-downloaded AND the .html is already on
            # disk (idempotent re-runs stay cheap). Conversion failure is
            # non-fatal — write_attachment_page falls back to the iframe.
            if target_filename.lower().endswith(".pdf"):
                html_filename = target_filename + ".html"
                kept_filenames.add(html_filename)
                pdf_unchanged = bool(
                    old and old.get("filename") == target_filename
                )
                html_path = os.path.join(adir, html_filename)
                needs_convert = not (pdf_unchanged and os.path.exists(html_path))
                if needs_convert:
                    ok = convert_pdf_to_html(
                        os.path.join(adir, target_filename), html_path
                    )
                    if ok:
                        print(
                            f"  Article {article['articleNumber']:>2}: "
                            f"converted {target_filename} → {html_filename}"
                        )
                    else:
                        print(
                            f"  Article {article['articleNumber']:>2}: "
                            f"WARNING: pdf2htmlEX conversion failed for "
                            f"{target_filename}; falling back to iframe view"
                        )
                att["htmlFilename"] = html_filename if os.path.exists(html_path) else None
            else:
                att["htmlFilename"] = None

            # Extract /URI links with their visible anchor text for the
            # sidebar nav. Manifest format is [{"url": ..., "label": ...}].
            # Reuse the cached entries when the PDF is unchanged AND the
            # cache is in the new dict-shape (legacy string lists are
            # rebuilt). Falls back to the raw-PDF binary scan when no HTML
            # render is available — labels are then unknown so the URL
            # itself is used.
            cached = old.get("pdfLinks") if old else None
            cache_is_new_shape = (
                isinstance(cached, list) and (not cached or isinstance(cached[0], dict))
            )
            if (
                old
                and old.get("filename") == target_filename
                and cache_is_new_shape
            ):
                att["pdfLinks"] = list(cached)
            elif att.get("htmlFilename"):
                att["pdfLinks"] = [
                    {"url": u, "label": lbl}
                    for lbl, u in extract_html_links(
                        os.path.join(adir, att["htmlFilename"])
                    )
                ]
            elif target_filename.lower().endswith(".pdf"):
                att["pdfLinks"] = [
                    {"url": u, "label": u}
                    for u in extract_pdf_uris(os.path.join(adir, target_filename))
                ]
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

        # Remove anything we didn't just generate or download. Each
        # attachment is now a subdirectory (slug/), so cleanup must
        # rmtree directories that aren't in the current attachment set.
        preserved = set(kept_filenames)
        preserved.update({"index.md", ".pages"})
        preserved.update(s for s, _, _ in att_pages)
        for entry in os.listdir(adir):
            path = os.path.join(adir, entry)
            if entry.startswith(".pending-"):
                os.remove(path)
                continue
            if entry in preserved:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"  Article {article['articleNumber']:>2}: removed stale {entry}")

    for entry in os.listdir(articles_dir):
        full = os.path.join(articles_dir, entry)
        if os.path.isdir(full) and entry not in seen_dirs:
            shutil.rmtree(full)
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
    write_root_pages(archive_dir, articles)
    write_readme(archive_dir)
    print(f"\nWrote manifest to {manifest_path}")
    print(f"Archive root: {archive_dir}")


def sync_progress_only():
    """Refresh article dispositions from the Moderator's progress sheet.

    Fast-path mode for the live-meeting cadence: skip the primegov fetch,
    skip every PDF download and pdf2htmlEX run, and only update fields
    sourced from the progress tracker (status, disposition). Per-article
    summary pages get re-rendered because the disposition admonition
    lives there, and the root INDEX.md + .pages are regenerated because
    articles move between the disposed / deferred / pending groups as
    outcomes change.

    Requires a prior full sync — bails if index.json is absent, since
    the manifest is the article list source of truth in this mode.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    archive_dir = os.path.join(project_root, ARCHIVE_DIR)
    articles_dir = os.path.join(archive_dir, ARTICLES_SUBDIR)
    manifest_path = os.path.join(archive_dir, "index.json")

    if not os.path.exists(manifest_path):
        print("ERROR: index.json not found; run a full sync first.", file=sys.stderr)
        sys.exit(1)
    with open(manifest_path) as fh:
        prior = json.load(fh)
    articles = prior.get("articles", [])
    print(f"Loaded {len(articles)} articles from index.json")

    progress = fetch_article_progress()
    for a in articles:
        entry = progress.get(a["articleNumber"], {"status": "pending"})
        a["status"] = entry["status"]
        if entry.get("disposition"):
            a["disposition"] = entry["disposition"]
        else:
            # An article can move back to pending if the sheet entry
            # is cleared. Full sync starts from a fresh dict so it
            # naturally drops the key; progress-only mode has to do
            # it explicitly because articles came from the manifest.
            a.pop("disposition", None)
    disposed_count = sum(1 for a in articles if a["status"] == "disposed")
    print(f"  {disposed_count} disposed, {len(articles) - disposed_count} pending")

    for article in articles:
        adir = os.path.join(articles_dir, article_dirname(article))
        if not os.path.isdir(adir):
            continue
        att_pages = attachment_pages_for(article)
        write_article_summary(adir, article, att_pages)
        # Per-attachment markdown pages are gitignored (regenerated build
        # artifacts), so a fresh CI checkout has none. The workflow caches
        # the pdf2htmlEX HTML between runs; here we splice it back into
        # the markdown wrapper. On cache miss for a particular PDF,
        # write_attachment_page falls back to an iframe view — degraded
        # but functional until the next full sync repaves it.
        for slug, name, att in att_pages:
            write_attachment_page(adir, slug, name, att)

    synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "source": SOURCE_URL,
        "meetingTemplateId": MEETING_TEMPLATE_ID,
        "lastSynced": synced_at,
        "articleCount": len(articles),
        "articles": articles,
    }
    # Same no-op suppression as the full sync: don't bump the timestamp
    # if nothing else changed. Keeps the working tree clean on uneventful
    # runs so the workflow's `git status --porcelain` check short-circuits.
    prior_ts = prior.get("lastSynced")
    if prior_ts and {k: v for k, v in prior.items() if k != "lastSynced"} == \
                   {k: v for k, v in manifest.items() if k != "lastSynced"}:
        manifest["lastSynced"] = prior_ts
        synced_at = prior_ts

    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    write_index_md(archive_dir, articles, synced_at)
    write_root_pages(archive_dir, articles)
    print(f"\nWrote manifest to {manifest_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Sync the Arlington Town Meeting Annotated Warrant."
    )
    parser.add_argument(
        "--progress-only",
        action="store_true",
        help="Refresh only article dispositions from the progress sheet, "
             "skipping primegov fetch and PDF conversion. Requires a prior "
             "full sync.",
    )
    args = parser.parse_args()
    if args.progress_only:
        sync_progress_only()
    else:
        sync()
