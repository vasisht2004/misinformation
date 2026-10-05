#!/usr/bin/env python3
"""Find the cheapest legitimate way to list fact-check articles (2022–2025).

This is a reconnaissance probe, not a scraper. For each site it asks only
for documents that are meant to be machine-readable indexes:

* robots.txt
* sitemap indexes and sitemap urlsets (URLs only, never the articles)
* the WordPress REST index (one post record, plus category search)
* RSS/Atom feeds (item counts only)
* the homepage, so a bot wall is visible before anyone plans a crawl

It does not request an article URL, does not parse article text, and does
not retry, rotate user-agents, run challenge JavaScript, or otherwise try
to get past a block. A challenge or a hard block is written down and that
URL is left alone.

Politeness, per the brief:

* one normal browser User-Agent for the whole run
* 20 second timeout
* 2 seconds between requests
* at most 30 HTTP requests per site

Run from anywhere:

    python recon.py

Writes recon_output/ next to this file. The three recommended routes are
printed on stdout; progress goes to stderr.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Fixed scope. These three publishers are the whole job; paths below are the
# well-known listing endpoints, not article pages.
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "recon_output"

SITES = (
    {"slug": "boom", "base": "https://www.boomlive.in"},
    {"slug": "newsmobile", "base": "https://newsmobile.in"},
    {"slug": "thip", "base": "https://thip.media"},
)

# Tried on every site, then anything else declared in robots.txt.
SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/post-sitemap.xml",
    "/news-sitemap.xml",
)

RSS_PATHS = ("/feed", "/rss", "/feed/rss")

# The listing window we care about. Year coverage is read from sitemap
# *filenames* (and only those). Fetching every child sitemap to inspect
# <lastmod> would blow the 30-request cap and is not required.
TARGET_YEARS = (2022, 2023, 2024, 2025)

# A current desktop Chrome UA. Sent on every request and never swapped
# after a block — swapping it would be an attempt to get around a wall.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

SLEEP_SECONDS = 2
REQUEST_TIMEOUT = 20
MAX_REQUESTS_PER_SITE = 30

# Child urlsets are fetched only to collect 5 sample article URLs. The
# index already tells us how children are split and which years appear in
# their names, so we do not walk the tree.
MAX_CHILD_SITEMAP_FETCHES = 3
SAMPLE_ARTICLE_LIMIT = 5

# Leave this many requests unspent while reading extra robots sitemaps, so
# the sample-url fetches still fit under the cap.
CHILD_FETCH_RESERVE = MAX_CHILD_SITEMAP_FETCHES

# Homepage (and HTML error pages) are scanned for these, in this order.
# First match wins. Matching is case-insensitive because challenge pages
# do not agree on capitalization.
CHALLENGE_MARKERS = (
    "Anubis",
    "cf-challenge",
    "Just a moment",
    "captcha",
    "Access denied",
    "Checking your browser",
)

# Status codes that mean "do not continue against this URL". 404 is absent
# on purpose: a missing sitemap is information, not a block.
HARD_BLOCK_STATUSES = {401, 403, 429, 503}

# Filename tokens that mean a sitemap is split by content type rather than
# by month. "post-sitemap1.xml" is one type; "post" plus "page" is a split.
SITEMAP_TYPE_TOKENS = (
    "post",
    "page",
    "category",
    "tag",
    "author",
    "news",
    "video",
    "image",
    "product",
    "attachment",
)

# 2024, 2024-01, 2024_01, 202401. The month group is optional so a bare
# year still counts. It will not treat the "1" in post-sitemap1.xml as a date.
DATE_IN_NAME = re.compile(r"(20\d{2})(?:[-_]?(0[1-9]|1[0-2]))?")

# How many Disallow paths to spell out in the markdown cell. The saved
# robots file keeps every line.
MAX_DISALLOW_LISTED = 40

# Routes the report is allowed to recommend, best listing method first.
# archive-crawl and blocked-needs-permission are fallbacks, not preferences.
ROUTE_WPJSON = "wpjson"
ROUTE_SITEMAP = "sitemap"
ROUTE_RSS = "rss"
ROUTE_ARCHIVE = "archive-crawl"
ROUTE_BLOCKED = "blocked-needs-permission"

# Set once any HTTP request has been sent, so the 2 second gap also applies
# between the last request of one site and the first request of the next.
_any_request_sent = False


@dataclass
class FetchResult:
    """One HTTP attempt. `skipped` means we did not touch the network."""

    requested_url: str
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    text: str = ""
    wp_total: str | None = None
    wp_total_pages: str | None = None
    error: str | None = None
    skipped: bool = False
    challenge: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.skipped and not self.error and not self.challenge


@dataclass
class ProbeRow:
    probe: str
    status: str
    finding: str


@dataclass
class SiteReport:
    slug: str
    base: str
    rows: list[ProbeRow] = field(default_factory=list)
    route: str = ROUTE_ARCHIVE
    route_reason: str = ""
    requests_made: int = 0
    homepage_status: str = ""
    bot_challenge: str = ""
    robots_status: str = ""
    disallow_count: int = 0
    robots_sitemaps: str = ""
    sitemap_status: str = ""
    child_sitemap_count: int = 0
    sitemap_split: str = ""
    years_covered: str = ""
    years_missing: str = ""
    sample_article_urls: str = ""
    wp_posts_status: str = ""
    wp_total: str = ""
    wp_total_pages: str = ""
    wp_usable: bool = False
    fact_categories: str = ""
    health_categories: str = ""
    rss_feed_status: str = ""
    rss_feed_items: str = ""
    rss_rss_status: str = ""
    rss_rss_items: str = ""
    rss_feed_rss_status: str = ""
    rss_feed_rss_items: str = ""
    sitemap_usable: bool = False
    rss_usable: bool = False
    homepage_open: bool = False


class PoliteClient:
    """GET helper with the per-site cap, the gap, and no retry.

    A transport error still counts as a request: the cap limits attempts,
    not just successful responses. Skipped calls (cap already reached) do
    not sleep and do not increment the counter.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                # Ordinary browser Accept. Not changed after a block.
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.used = 0

    def get(self, url: str) -> FetchResult:
        global _any_request_sent

        if self.used >= MAX_REQUESTS_PER_SITE:
            return FetchResult(requested_url=url, skipped=True)

        # Gap is between requests, so the first call of the process does
        # not wait. Later calls wait even when the previous one failed.
        if _any_request_sent:
            time.sleep(SLEEP_SECONDS)

        self.used += 1
        _any_request_sent = True

        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            # No retry. A timeout or a dropped connection is recorded as-is.
            return FetchResult(
                requested_url=url,
                error=f"{type(exc).__name__}: {exc}",
            )

        return FetchResult(
            requested_url=url,
            final_url=response.url,
            status=response.status_code,
            content_type=response.headers.get("Content-Type", ""),
            text=_response_text(response),
            wp_total=response.headers.get("X-WP-Total"),
            wp_total_pages=response.headers.get("X-WP-TotalPages"),
        )


def _response_text(response: requests.Response) -> str:
    """Decode the body. Unwrap gzip when the server forgot Content-Encoding.

    requests already decompresses a proper Content-Encoding. Some sitemap
    URLs are raw .gz bytes with that header missing; those still need a
    manual decompress before the XML tags can be read. This is decoding,
    not a second request.
    """
    data = response.content
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    encoding = response.encoding or "utf-8"
    return data.decode(encoding, errors="replace").lstrip("\ufeff")


def log(message: str) -> None:
    """Progress on stderr so stdout stays the three route lines."""
    print(message, file=sys.stderr, flush=True)


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def canonical_url(url: str) -> str:
    """Identity used only to avoid fetching the same sitemap twice.

    Scheme and host are compared case-insensitively. The path keeps its
    case. A fragment and a trailing slash do not make a second request.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def absolute_url(document_url: str, loc: str) -> str:
    """Resolve a sitemap <loc> against the file it was found in.

    Protocol-relative locs (//cdn.example/sitemap.xml) keep the document's
    scheme. They are still just sitemap addresses, not article pages.
    """
    loc = "".join(loc.split())
    if not loc:
        return ""
    parts = urlsplit(loc)
    base = urlsplit(document_url)
    if parts.netloc:
        scheme = parts.scheme or base.scheme
        return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    path = parts.path
    if path.startswith("/"):
        return urlunsplit((base.scheme, base.netloc, path, parts.query, ""))
    parent = base.path.rsplit("/", 1)[0]
    return urlunsplit((base.scheme, base.netloc, f"{parent}/{path}", parts.query, ""))


def challenge_marker(text: str) -> str | None:
    """Return the first challenge keyword in `text`, if any."""
    haystack = text.lower()
    for marker in CHALLENGE_MARKERS:
        if marker.lower() in haystack:
            return marker
    return None


def maybe_challenge(result: FetchResult, *, always: bool) -> None:
    """Mark a challenge page. Never called in order to retry the URL.

    Sitemap XML and RSS are not scanned for these words unless the response
    is HTML or already a hard block. An article slug that happens to contain
    "captcha" must not be reported as a wall.
    """
    if result.skipped or result.error or not result.text:
        return
    prefix = result.text[:20000]
    stripped = prefix.lstrip()
    htmlish = "html" in result.content_type.lower() or (
        stripped.startswith("<") and not stripped.startswith("<?xml")
    )
    if always or htmlish or result.status in HARD_BLOCK_STATUSES:
        result.challenge = challenge_marker(prefix)


def status_label(result: FetchResult) -> str:
    if result.skipped:
        return "skipped"
    if result.error:
        return "error"
    if result.status is None:
        return "error"
    return str(result.status)


def redirect_note(result: FetchResult) -> str:
    if not result.final_url:
        return ""
    if canonical_url(result.final_url) == canonical_url(result.requested_url):
        return ""
    return f" Redirected to {result.final_url}."


def md_cell(value: str) -> str:
    """Keep a markdown table cell on one line."""
    return value.replace("|", "\\|").replace("\n", " ").strip()


def clip_list(items: list[str], limit: int) -> str:
    if not items:
        return "(none)"
    if len(items) <= limit:
        return ", ".join(items)
    shown = ", ".join(items[:limit])
    return f"{shown}, … ({len(items) - limit} more; full file saved)"


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def parse_robots(text: str) -> tuple[list[str], list[str]]:
    """Pull Disallow paths and Sitemap URLs. Comments and other directives
    are ignored. A blank Disallow (which means "allow all" for that group)
    is kept so the report does not hide it.
    """
    disallows: list[str] = []
    sitemaps: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "disallow":
            disallows.append(value if value else "(empty — allow all)")
        elif key == "sitemap" and value:
            sitemaps.append(value)
    return disallows, sitemaps


def looks_like_robots(text: str) -> bool:
    head = text[:2000].lower()
    return "user-agent" in head or "disallow:" in head or "sitemap:" in head


# ---------------------------------------------------------------------------
# Sitemaps. BeautifulSoup's html.parser is enough: sitemap tags are
# lowercase-able and we only need <sitemap>, <url>, and <loc>. Article
# <loc> values are recorded, never requested.
# ---------------------------------------------------------------------------

def _loc_text(node: BeautifulSoup) -> str:
    return "".join(node.get_text().split())


def parse_sitemap(xml_text: str, document_url: str) -> dict:
    """Return child-sitemap URLs and article URLs from one sitemap document.

    An index has <sitemap><loc> children. A urlset has <url><loc> entries.
    If both appear, the file is treated as an index and the url locs are
    ignored for sampling — they are usually nested metadata, not a mix we
    should half-crawl.
    """
    soup = BeautifulSoup(xml_text, "html.parser")
    children: list[str] = []
    for node in soup.find_all("sitemap"):
        loc = node.find("loc")
        if loc is None:
            continue
        url = absolute_url(document_url, _loc_text(loc))
        if url:
            children.append(url)

    articles: list[str] = []
    if not children:
        for node in soup.find_all("url"):
            loc = node.find("loc")
            if loc is None:
                continue
            url = absolute_url(document_url, _loc_text(loc))
            if url and _is_article_url(url):
                articles.append(url)

    if children:
        kind = "index"
    elif articles:
        kind = "urlset"
    else:
        kind = "unknown"
    return {"kind": kind, "children": children, "articles": articles}


def _is_article_url(url: str) -> bool:
    """Sitemap locs that are themselves sitemaps are not article samples."""
    path = urlsplit(url).path.lower()
    return not (path.endswith(".xml") or path.endswith(".xml.gz"))


def sitemap_filename(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1].lower()
    return name or path.lower()


def years_in_sitemap_names(urls: list[str]) -> set[int]:
    found: set[int] = set()
    for url in urls:
        for match in DATE_IN_NAME.finditer(sitemap_filename(url)):
            year = int(match.group(1))
            if year in TARGET_YEARS:
                found.add(year)
    return found


def describe_split(child_urls: list[str]) -> str:
    """Say whether child *names* are partitioned by date, by type, or neither.

    Numbered Yoast shards (post-sitemap1.xml, post-sitemap2.xml) are one
    type, not a date split. Date requires a year or year-month in the name.
    Type requires two different tokens (post and page, for example).
    """
    if not child_urls:
        return "n/a (no child sitemaps)"

    names = [sitemap_filename(url) for url in child_urls]
    dated = any(DATE_IN_NAME.search(name) for name in names)
    types: set[str] = set()
    for name in names:
        for token in SITEMAP_TYPE_TOKENS:
            if token in name:
                types.add(token)
                break

    if dated and len(types) >= 2:
        return "split by date and type"
    if dated:
        return "split by date"
    if len(types) >= 2:
        return "split by type"
    if len(child_urls) > 1:
        return "not split by date or type (numbered shards or opaque names)"
    return "not split by date or type (single child)"


def describe_years(found: set[int]) -> tuple[str, str, str]:
    """Return (covered csv, missing csv, a sentence for the report).

    `missing` is filled only when at least one target year appears in a
    name. An undated name (post-sitemap1.xml) does not mean 2022–2025 are
    absent; it means the filename does not say.
    """
    covered = [year for year in TARGET_YEARS if year in found]
    missing = [year for year in TARGET_YEARS if year not in found] if found else []
    covered_s = ", ".join(str(year) for year in covered)
    missing_s = ", ".join(str(year) for year in missing)
    if not found:
        sentence = "no child-sitemap name contains a year in 2022–2025"
    elif not missing:
        sentence = "child-sitemap names cover 2022, 2023, 2024, 2025"
    else:
        sentence = (
            f"child-sitemap names cover {covered_s}; "
            f"missing {missing_s}"
        )
    return covered_s, missing_s, sentence


def rank_child_sitemap(url: str) -> tuple:
    """Sort key: post/news files in 2022–2025 first; page/author/tag last.

    Lower sorts first. We only open a few children, so this pick is what
    makes the 5 samples likely to be articles rather than author archives.
    """
    name = sitemap_filename(url)
    deprioritize = int(
        any(token in name for token in ("page", "author", "category", "tag", "image", "video"))
    )
    prefer_post = 0 if ("post" in name or "news" in name) else 1
    prefer_year = 0 if years_in_sitemap_names([url]) else 1
    return (deprioritize, prefer_post, prefer_year, name)


def take_samples(urls: list[str], sink: list[str]) -> None:
    for url in urls:
        if len(sink) >= SAMPLE_ARTICLE_LIMIT:
            return
        if url not in sink:
            sink.append(url)


# ---------------------------------------------------------------------------
# WordPress REST and RSS. The posts call uses the exact query from the
# brief (per_page=1). The body of that one post is not saved and its
# title/content are not read; we only check that the payload is JSON and
# whether the response is an HTML challenge page.
# ---------------------------------------------------------------------------

def looks_like_json(result: FetchResult) -> bool:
    if "json" in result.content_type.lower():
        return True
    stripped = result.text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def parse_categories(result: FetchResult) -> list[dict]:
    """Category id, name, and count. Nothing else from the payload."""
    if not looks_like_json(result):
        return []
    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    categories = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        categories.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "count": item.get("count"),
            }
        )
    return categories


def format_categories(categories: list[dict]) -> str:
    if not categories:
        return "(none)"
    parts = []
    for category in categories:
        name = category.get("name") or "?"
        parts.append(
            f"{name} (id={category.get('id')}, count={category.get('count')})"
        )
    return "; ".join(parts)


def count_feed_items(xml_text: str) -> int:
    """Count RSS <item> or Atom <entry> elements. Titles and descriptions
    are not collected.
    """
    soup = BeautifulSoup(xml_text, "html.parser")
    items = soup.find_all("item")
    if items:
        return len(items)
    return len(soup.find_all("entry"))


# ---------------------------------------------------------------------------
# Per-site probe
# ---------------------------------------------------------------------------

def add_row(report: SiteReport, probe: str, result: FetchResult, finding: str) -> None:
    if result.challenge and not finding.startswith("bot challenge"):
        finding = f"bot challenge ({result.challenge}). {finding}"
    finding = (finding + redirect_note(result)).strip()
    report.rows.append(
        ProbeRow(probe=probe, status=status_label(result), finding=finding)
    )
    log(f"[{report.slug}] {probe} -> {status_label(result)}")


def skipped_row(report: SiteReport, probe: str) -> None:
    report.rows.append(
        ProbeRow(
            probe=probe,
            status="skipped",
            finding=f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached",
        )
    )
    log(f"[{report.slug}] {probe} -> skipped")


def recon_site(site: dict) -> SiteReport:
    slug = site["slug"]
    base = site["base"]
    report = SiteReport(slug=slug, base=base)
    client = PoliteClient()
    log(f"[{slug}] start {base}")

    _probe_homepage(report, client)
    robots_sitemaps = _probe_robots(report, client)
    _probe_wp_posts(report, client)
    _probe_wp_categories(report, client, "fact")
    _probe_wp_categories(report, client, "health")
    _probe_rss(report, client)
    _probe_sitemaps(report, client, robots_sitemaps)

    report.requests_made = client.used
    _recommend(report)
    log(f"[{slug}] route {report.route} ({report.requests_made} requests)")
    return report


def _probe_homepage(report: SiteReport, client: PoliteClient) -> None:
    result = client.get(report.base + "/")
    # Always scan the homepage. A 200 challenge page is still a wall.
    maybe_challenge(result, always=True)
    _save_text(
        OUTPUT_DIR / f"{report.slug}_home.html",
        result.text[:2000] if result.text else _empty_body_note(result),
    )
    report.homepage_status = status_label(result)
    report.bot_challenge = result.challenge or ""
    report.homepage_open = bool(result.ok)
    if result.skipped:
        finding = f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached"
    elif result.error:
        finding = result.error
    elif result.challenge:
        finding = f"bot challenge ({result.challenge})"
    else:
        finding = "no bot-challenge markers in the response"
    add_row(report, "homepage", result, finding)


def _probe_robots(report: SiteReport, client: PoliteClient) -> list[str]:
    url = join_url(report.base, "/robots.txt")
    result = client.get(url)
    maybe_challenge(result, always=False)
    _save_text(
        OUTPUT_DIR / f"{report.slug}_robots.txt",
        result.text if result.text else _empty_body_note(result),
    )
    report.robots_status = status_label(result)

    disallows: list[str] = []
    sitemaps: list[str] = []
    if result.skipped:
        finding = f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached"
    elif result.error:
        finding = result.error
    elif result.challenge:
        finding = (
            f"bot challenge ({result.challenge}); "
            "Disallow and Sitemap lines were not parsed"
        )
    elif not looks_like_robots(result.text):
        finding = "body does not look like robots.txt; Disallow and Sitemap not parsed"
    else:
        disallows, sitemaps = parse_robots(result.text)
        report.disallow_count = len(disallows)
        report.robots_sitemaps = " | ".join(sitemaps)
        finding = (
            f"Disallow ({len(disallows)}): {clip_list(disallows, MAX_DISALLOW_LISTED)}. "
            f"Sitemap entries ({len(sitemaps)}): {clip_list(sitemaps, 20)}"
        )
    add_row(report, "robots.txt", result, finding)
    return sitemaps


def _probe_wp_posts(report: SiteReport, client: PoliteClient) -> None:
    url = join_url(report.base, "/wp-json/wp/v2/posts?per_page=1")
    result = client.get(url)
    maybe_challenge(result, always=False)
    report.wp_posts_status = status_label(result)
    report.wp_total = result.wp_total or ""
    report.wp_total_pages = result.wp_total_pages or ""

    if result.skipped:
        finding = f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached"
    elif result.error:
        finding = result.error
    elif result.challenge:
        finding = f"bot challenge ({result.challenge})"
    elif result.status == 200 and looks_like_json(result):
        # Usable even when a proxy strips the total headers. The headers
        # are still what the brief asked to report.
        report.wp_usable = True
        total = result.wp_total if result.wp_total is not None else "(header absent)"
        pages = result.wp_total_pages if result.wp_total_pages is not None else "(header absent)"
        finding = f"JSON posts index. X-WP-Total={total}; X-WP-TotalPages={pages}"
    else:
        finding = (
            f"not a usable posts index "
            f"(content-type {result.content_type or 'unknown'})"
        )
    add_row(report, "wp-json posts?per_page=1", result, finding)


def _probe_wp_categories(report: SiteReport, client: PoliteClient, term: str) -> None:
    url = join_url(report.base, f"/wp-json/wp/v2/categories?search={term}&per_page=20")
    result = client.get(url)
    maybe_challenge(result, always=False)
    probe = f"wp-json categories?search={term}"

    if result.skipped:
        formatted = ""
        finding = f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached"
    elif result.error:
        formatted = ""
        finding = result.error
    elif result.challenge:
        formatted = ""
        finding = f"bot challenge ({result.challenge})"
    elif result.status == 200 and looks_like_json(result):
        categories = parse_categories(result)
        formatted = format_categories(categories)
        finding = f"{len(categories)} categor{'y' if len(categories) == 1 else 'ies'}: {formatted}"
    else:
        formatted = ""
        finding = f"no category list (content-type {result.content_type or 'unknown'})"

    if term == "fact":
        report.fact_categories = formatted
    else:
        report.health_categories = formatted
    add_row(report, probe, result, finding)


def _probe_rss(report: SiteReport, client: PoliteClient) -> None:
    # Column map for the summary CSV. One feed with items is enough to call
    # RSS usable; the historical window is usually NOT covered by a feed,
    # which is why RSS ranks behind wp-json and sitemaps.
    columns = {
        "/feed": ("rss_feed_status", "rss_feed_items"),
        "/rss": ("rss_rss_status", "rss_rss_items"),
        "/feed/rss": ("rss_feed_rss_status", "rss_feed_rss_items"),
    }
    for path in RSS_PATHS:
        result = client.get(join_url(report.base, path))
        maybe_challenge(result, always=False)
        status_attr, items_attr = columns[path]
        setattr(report, status_attr, status_label(result))

        if result.skipped:
            finding = f"not requested; {MAX_REQUESTS_PER_SITE}-request cap reached"
        elif result.error:
            finding = result.error
        elif result.challenge:
            finding = f"bot challenge ({result.challenge})"
        elif result.status == 200:
            count = count_feed_items(result.text)
            setattr(report, items_attr, str(count))
            if count > 0:
                report.rss_usable = True
            finding = f"{count} RSS/Atom items"
        else:
            finding = "no feed body counted"
        add_row(report, path, result, finding)


def _probe_sitemaps(
    report: SiteReport,
    client: PoliteClient,
    robots_sitemaps: list[str],
) -> None:
    """Fetch listing sitemaps, then at most three children for sample URLs.

    Year coverage and the date/type split come from child filenames inside
    indexes we already downloaded. Sample article URLs are the first five
    <loc> values that are not themselves sitemap files.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_candidate(label: str, url: str) -> None:
        key = canonical_url(url)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append((label, url))

    for path in SITEMAP_PATHS:
        add_candidate(path, join_url(report.base, path))
    for sitemap_url in robots_sitemaps:
        add_candidate(f"robots Sitemap {sitemap_url}", absolute_url(report.base + "/", sitemap_url))

    all_children: list[str] = []
    samples: list[str] = []
    best_status = ""
    parsed_ok = False
    saw_urlset = False
    # Requested URL and any redirect target. Child sampling must not spend
    # a second request on a file the candidate loop already parsed.
    fetched: set[str] = set()

    standard_labels = set(SITEMAP_PATHS)
    for label, url in candidates:
        # Well-known paths are part of the brief. Extra robots sitemaps stop
        # once the child-sample reserve would be eaten.
        if label not in standard_labels and client.used >= MAX_REQUESTS_PER_SITE - CHILD_FETCH_RESERVE:
            skipped_row(report, label)
            continue

        result = client.get(url)
        if result.skipped:
            # Cap reached. Do not mark the URL fetched, and do not call
            # get() again for the rest of this list.
            skipped_row(report, label)
            for later_label, _later_url in candidates[candidates.index((label, url)) + 1 :]:
                skipped_row(report, later_label)
            break
        fetched.add(canonical_url(url))
        if result.final_url:
            fetched.add(canonical_url(result.final_url))
        maybe_challenge(result, always=False)

        # A later real sitemap overwrites this. An HTML 200 that is not a
        # sitemap must not be the status we remember once a urlset exists.
        if best_status != "200":
            best_status = status_label(result)

        if result.error:
            add_row(report, label, result, result.error)
            continue
        if result.challenge:
            add_row(report, label, result, f"bot challenge ({result.challenge})")
            continue
        if result.status != 200:
            add_row(report, label, result, "not a sitemap document")
            continue

        document_url = result.final_url or url
        parsed = parse_sitemap(result.text, document_url)
        if parsed["kind"] == "unknown":
            add_row(
                report,
                label,
                result,
                "200 but no sitemap index and no urlset (not used as a listing)",
            )
            continue

        parsed_ok = True
        best_status = "200"
        if parsed["kind"] == "urlset":
            saw_urlset = True
        all_children.extend(parsed["children"])
        take_samples(parsed["articles"], samples)
        add_row(
            report,
            label,
            result,
            _sitemap_finding(parsed, samples_from_here=parsed["articles"][:SAMPLE_ARTICLE_LIMIT]),
        )

    # Dedupe children across sitemap.xml and sitemap_index.xml when both
    # list the same files, so the count is not doubled.
    unique_children = list(dict.fromkeys(all_children))
    # Names seen one level down, filled in if a fetched child is itself an
    # index. Direct-child count stays `unique_children`; years use both.
    nested_names: list[str] = []
    _fetch_sample_children(report, client, unique_children, samples, fetched, nested_names)

    split = describe_split(unique_children) if unique_children else ""
    covered, missing, year_sentence = describe_years(
        years_in_sitemap_names(unique_children + nested_names)
    )
    if years_in_sitemap_names(nested_names) - years_in_sitemap_names(unique_children):
        year_sentence += " (extra years come from a nested index opened for samples)"

    report.sitemap_status = best_status
    report.child_sitemap_count = len(unique_children)
    if unique_children:
        report.sitemap_split = split
    elif saw_urlset:
        report.sitemap_split = "n/a (urlset, not an index)"
    report.years_covered = covered
    report.years_missing = missing
    report.sample_article_urls = " | ".join(samples)
    report.sitemap_usable = parsed_ok

    if unique_children or samples:
        # One summary row so the year/split answer is visible even when it
        # was assembled from several sitemap files.
        report.rows.append(
            ProbeRow(
                probe="sitemap summary",
                status=best_status or "n/a",
                finding=(
                    f"{len(unique_children)} unique child sitemaps; "
                    f"{report.sitemap_split or 'n/a'}; "
                    f"{year_sentence}. "
                    f"Sample article URLs ({len(samples)}): "
                    + (", ".join(samples) if samples else "(none collected)")
                ),
            )
        )


def _sitemap_finding(parsed: dict, samples_from_here: list[str]) -> str:
    if parsed["kind"] == "index":
        children = parsed["children"]
        _, _, year_sentence = describe_years(years_in_sitemap_names(children))
        return (
            f"sitemap index; {len(children)} child sitemaps; "
            f"{describe_split(children)}; {year_sentence}"
        )
    shown = ", ".join(samples_from_here) if samples_from_here else "(none)"
    return (
        f"urlset; 0 child sitemaps; {len(parsed['articles'])} article URLs; "
        f"sample article URLs: {shown}"
    )


def _fetch_sample_children(
    report: SiteReport,
    client: PoliteClient,
    children: list[str],
    samples: list[str],
    fetched: set[str],
    nested_names: list[str],
) -> None:
    """Open a few child urlsets until we have 5 article URLs or hit the cap.

    Files already parsed in the candidate loop are skipped. If a child is
    another index, its child filenames are recorded for the year check and
    one grandchild is queued. That grandchild still counts toward the three
    extra fetches. The rest of a nested index is not walked.
    """
    if len(samples) >= SAMPLE_ARTICLE_LIMIT or not children:
        return

    opened = 0
    queue = [url for url in sorted(children, key=rank_child_sitemap) if canonical_url(url) not in fetched]
    queued = {canonical_url(url) for url in queue}

    while queue and opened < MAX_CHILD_SITEMAP_FETCHES and len(samples) < SAMPLE_ARTICLE_LIMIT:
        url = queue.pop(0)
        if canonical_url(url) in fetched:
            continue
        result = client.get(url)
        if result.skipped:
            # The cap applies to every later call too. Stop, do not emit a
            # skipped row for each remaining child.
            skipped_row(report, f"child sitemap {url}")
            break
        fetched.add(canonical_url(url))
        if result.final_url:
            fetched.add(canonical_url(result.final_url))
        opened += 1
        maybe_challenge(result, always=False)

        if result.error or result.challenge or result.status != 200:
            detail = result.error or (
                f"bot challenge ({result.challenge})" if result.challenge else "not a sitemap document"
            )
            add_row(report, f"child sitemap {url}", result, detail)
            # A challenged child is not a door to its grandchildren.
            continue

        parsed = parse_sitemap(result.text, result.final_url or url)
        if parsed["kind"] == "urlset":
            before = len(samples)
            take_samples(parsed["articles"], samples)
            gained = samples[before:]
            shown = ", ".join(gained) if gained else "(none new)"
            add_row(
                report,
                f"child sitemap {url}",
                result,
                f"urlset; {len(parsed['articles'])} article URLs; samples taken: {shown}",
            )
        elif parsed["kind"] == "index":
            for child_url in parsed["children"]:
                if child_url not in nested_names:
                    nested_names.append(child_url)
            add_row(
                report,
                f"child sitemap {url}",
                result,
                (
                    f"nested index; {len(parsed['children'])} children; "
                    f"{describe_split(parsed['children'])}"
                ),
            )
            # One grandchild, pushed to the front, still counts against `opened`.
            if parsed["children"] and len(samples) < SAMPLE_ARTICLE_LIMIT:
                grandchild = sorted(parsed["children"], key=rank_child_sitemap)[0]
                key = canonical_url(grandchild)
                if key not in fetched and key not in queued:
                    queue.insert(0, grandchild)
                    queued.add(key)
        else:
            add_row(
                report,
                f"child sitemap {url}",
                result,
                "200 but no sitemap index and no urlset",
            )


def _recommend(report: SiteReport) -> None:
    """Pick one route. First match wins.

    wpjson is first because a date-and-category query lists fact-checks
    without downloading every URL on the site. A sitemap is next because
    it can cover 2022–2025 in a handful of files. RSS is last among the
    machine-readable options: a feed almost never reaches back to 2022,
    but it is still a real index when nothing better answered.

    blocked-needs-permission is used when we did not get any of those
    indexes AND the front door is a challenge, a hard block, or simply
    unreachable. archive-crawl is only for a site that answered as a
    normal page and published none of the indexes.
    """
    if report.wp_usable:
        report.route = ROUTE_WPJSON
        report.route_reason = (
            "WordPress posts endpoint returned JSON 200, so fact-checks can be "
            "listed by category and date without an HTML crawl."
        )
        return

    if report.sitemap_usable:
        report.route = ROUTE_SITEMAP
        year_bit = (
            f" Child names cover {report.years_covered}."
            if report.years_covered
            else " Child names do not encode 2022–2025, so a later pass still filters URLs by date."
        )
        report.route_reason = "A sitemap index or urlset was readable." + year_bit
        return

    if report.rss_usable:
        report.route = ROUTE_RSS
        report.route_reason = (
            "A feed returned items. Feeds usually hold only recent posts, "
            "so this may not reach back to 2022."
        )
        return

    blocked = (not report.homepage_open) or bool(report.bot_challenge)
    if blocked:
        report.route = ROUTE_BLOCKED
        if report.bot_challenge:
            why = f"homepage matched {report.bot_challenge}"
        elif report.homepage_status in {str(code) for code in HARD_BLOCK_STATUSES}:
            why = f"homepage status {report.homepage_status}"
        else:
            why = f"homepage status {report.homepage_status or 'unreachable'}"
        report.route_reason = (
            f"No usable wp-json, sitemap, or RSS listing, and {why}. "
            "Do not crawl until that block is permitted."
        )
        return

    report.route = ROUTE_ARCHIVE
    report.route_reason = (
        "The homepage responded without a challenge marker, and wp-json, "
        "sitemaps, and RSS did not provide a listing. A later polite crawl "
        "of public archive or category pages is the remaining route."
    )


def _empty_body_note(result: FetchResult) -> str:
    if result.skipped:
        return f"# not requested; {MAX_REQUESTS_PER_SITE}-request cap reached\n"
    if result.error:
        return f"# no body; {result.error}\n"
    return f"# no body; status={result.status}\n"


def _save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def write_report(reports: list[SiteReport]) -> None:
    lines = [
        "# Fact-check listing recon (2022–2025)",
        "",
        "Probes only: robots.txt, sitemaps, WordPress REST index, RSS, homepage.",
        "Article URLs found in sitemaps or feeds were not requested.",
        "A bot challenge was recorded and not retried.",
        f"Politeness: {SLEEP_SECONDS}s between requests, "
        f"max {MAX_REQUESTS_PER_SITE} requests per site, "
        f"{REQUEST_TIMEOUT}s timeout.",
        "",
    ]
    for report in reports:
        lines.extend(
            [
                f"## {report.slug}",
                "",
                f"Base: {report.base}",
                f"Requests made: {report.requests_made}",
                "",
                "| Probe | Status | Finding |",
                "| --- | --- | --- |",
            ]
        )
        for row in report.rows:
            lines.append(
                f"| {md_cell(row.probe)} | {md_cell(row.status)} | {md_cell(row.finding)} |"
            )
        lines.extend(
            [
                "",
                f"**Recommended route:** {report.route} — {report.route_reason}",
                "",
            ]
        )
    path = OUTPUT_DIR / "recon_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")


def write_summary(reports: list[SiteReport]) -> None:
    rows = []
    for report in reports:
        rows.append(
            {
                "site": report.slug,
                "base_url": report.base,
                "recommended_route": report.route,
                "route_reason": report.route_reason,
                "requests_made": report.requests_made,
                "homepage_status": report.homepage_status,
                "bot_challenge": report.bot_challenge,
                "robots_status": report.robots_status,
                "disallow_count": report.disallow_count,
                "robots_sitemaps": report.robots_sitemaps,
                "sitemap_status": report.sitemap_status,
                "child_sitemap_count": report.child_sitemap_count,
                "sitemap_split": report.sitemap_split,
                "years_covered": report.years_covered,
                "years_missing": report.years_missing,
                "sample_article_urls": report.sample_article_urls,
                "wp_posts_status": report.wp_posts_status,
                "wp_total": report.wp_total,
                "wp_total_pages": report.wp_total_pages,
                "fact_categories": report.fact_categories,
                "health_categories": report.health_categories,
                "rss_feed_status": report.rss_feed_status,
                "rss_feed_items": report.rss_feed_items,
                "rss_rss_status": report.rss_rss_status,
                "rss_rss_items": report.rss_rss_items,
                "rss_feed_rss_status": report.rss_feed_rss_status,
                "rss_feed_rss_items": report.rss_feed_rss_items,
            }
        )
    frame = pd.DataFrame(rows)
    frame.to_csv(OUTPUT_DIR / "recon_summary.csv", index=False)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    reports = [recon_site(site) for site in SITES]
    write_report(reports)
    write_summary(reports)
    log(f"wrote {OUTPUT_DIR / 'recon_report.md'}")
    log(f"wrote {OUTPUT_DIR / 'recon_summary.csv'}")
    for report in reports:
        print(f"{report.slug}: {report.route}")


if __name__ == "__main__":
    main()
