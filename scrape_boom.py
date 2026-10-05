#!/usr/bin/env python3
"""Collect BOOM English fact-checks published from 2022-01-01 through 2025-12-31.

Listing pages are the path form only:

* page 1: https://www.boomlive.in/fact-check
* page N (N >= 2): https://www.boomlive.in/fact-check/N

robots.txt disallows "?page=" URLs, so this script never builds one. Article
links on those pages look like /fact-check/<slug>-<numeric id>. The word
"captcha" appears in normal BOOM HTML and is not treated as a block.

A response is a real block when its status is 403 or 429, or when a non-404
body is under 20 KB and its <title> does not contain "BOOM". On a block the
checkpoints already written are left on disk and the process exits. It does
not rotate the User-Agent, retry the blocked URL, or otherwise try to get
around the wall.

Stages, in order:

* A. Drop the latest/trending sidebar, then binary-search the real listing
     for the 2022–2025 page range.
* B. Collect non-sidebar article URLs from that range, plus a two-page buffer.
* C. Scrape each article, checkpointing every 10.
* D. Write one CSV per year.

    python scrape_boom.py
    python scrape_boom.py --max-pages 3 --max-articles 5
    python scrape_boom.py --skip-range-search

Stdout is the per-year counts. Progress and probe logs go to stderr.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup
from lxml import html as lxml_html
from tqdm import tqdm


# ---------------------------------------------------------------------------
# Scope. English fact-checks live under /fact-check/. Other BOOM sections
# (Hindi, explainers, and so on) are a different archive and are ignored.
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "data" / "raw" / "boom"

BASE = "https://www.boomlive.in"
HOST = "www.boomlive.in"

WINDOW_START = "2022-01-01"
WINDOW_END = "2025-12-31"
# Validity search stays inside this page number. Pages past the archive
# still return HTTP 200, but only the sidebar links, so they are not valid.
SEARCH_FIRST_PAGE = 1
SEARCH_LAST_PAGE = 2000
# Pages on either side of the searched range. A listing page can straddle
# 1 Jan 2022 or 31 Dec 2025, and the search only dates one article per page.
# The buffer is clamped so it never walks past the last valid page.
PAGE_BUFFER = 2
# A real listing page has a run of stories plus the sidebar. Past the end
# of the archive the page is only the sidebar, which is fewer than this.
MIN_NON_SIDEBAR_LINKS = 5

# One matcher for every listing link. The optional subcategory and the slug
# use the same character class, so the group must be allowed to backtrack:
# /fact-check/foo-1234 is a slug, not a subcategory with nothing after it.
# The id is 4–6 digits. Pagination (/fact-check/2) and other sections
# (/tags, /weekly-wrap) do not match. Case is ignored.
ARTICLE_PATH_RE = re.compile(
    r"^/(?P<root>fact-check|fast-check)"
    r"(?:/(?P<subcategory>[a-z0-9-]+))?"
    r"/(?P<slug>[a-z0-9-]+)-(?P<id>\d{4,6})$",
    re.IGNORECASE,
)
# Saved on the range file. A file from the older /fact-check/<slug>-<id>
# matcher stopped the archive too early (around page 168) and must be
# recomputed, because subcategory and fast-check links now count.
LINK_FILTER = "fact-check-fast-check-subcategory-id-4-6"

# Normal desktop Chrome. Never swapped after a block.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

TIMEOUT_SECONDS = 20
# One initial request plus three retries. Backoff is the gap before each
# retry, and it is already longer than the 1–2s politeness floor.
RETRY_BACKOFF_SECONDS = (2, 4, 8)
MAX_ATTEMPTS = 1 + len(RETRY_BACKOFF_SECONDS)
POLITE_SLEEP = (1.0, 2.0)
BLOCK_BODY_BYTES = 20 * 1024
ARTICLE_CHECKPOINT_EVERY = 10

YEAR_COLUMNS = ["title", "url", "date", "author", "content", "section"]
URL_COLUMNS = ["listing_page", "url"]

RANGE_PATH = OUTPUT_DIR / "boom_page_range.json"
URLS_PATH = OUTPUT_DIR / "boom_urls.csv"
# Written only after every listing page in the current window has been
# collected with LINK_FILTER. An older boom_urls.csv has no stamp, so those
# pages are fetched again and the missing subcategory / fast-check URLs
# are appended. Already scraped ids are not fetched a second time.
URL_FILTER_STAMP = OUTPUT_DIR / "boom_url_filter.txt"
PROGRESS_PATH = OUTPUT_DIR / "boom_progress.jsonl"

TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*BOOM\s*$", re.IGNORECASE)
YMD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class CrawlAbort(Exception):
    """Stop the run. Checkpoints written before this point stay on disk.

    Raised for a real block and for a listing/range fetch that still failed
    after three retries. Article-level failures are stored in the progress
    file instead, so one dead URL does not throw away the run.
    """


class SiteBlocked(CrawlAbort):
    """403, 429, or a short page whose title is not BOOM's."""


@dataclass
class ArticleRecord:
    """One progress-file line. Year CSVs keep a subset of these fields."""

    url: str
    title: str = ""
    date: str = ""
    author: str = ""
    content: str = ""
    # fact-check, fact-check/<subcategory>, or fast-check. fast-check keeps
    # that label even when the URL has a subcategory.
    section: str = ""
    scrape_status: str = "failed"
    error: str = ""


@dataclass
class PageProbe:
    """Date of one listing page.

    The dated article is the non-sidebar link with the lowest numeric id.
    Ids increase over time, so that link is the oldest story on the page.
    """

    page: int
    listing_url: str
    article_url: str = ""
    article_id: int | None = None
    date: str = ""
    valid: bool = False
    non_sidebar_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def log(message: str) -> None:
    """Progress on stderr. tqdm.write keeps a progress bar intact."""
    tqdm.write(message, file=sys.stderr)


def listing_url(page: int) -> str:
    """Path-form listing URL. Page 1 has no numeric suffix.

    This is the only listing URL builder. It has no query string, because
    robots.txt disallows "?page=".
    """
    if page < 1:
        raise ValueError(f"listing page must be >= 1, got {page}")
    if page == 1:
        return f"{BASE}/fact-check"
    return f"{BASE}/fact-check/{page}"


@dataclass(frozen=True)
class ArticleLink:
    """One accepted story link. `section` is the CSV/progress value."""

    url: str
    article_id: int
    section: str


def parse_article_link(href: str, base_url: str) -> ArticleLink | None:
    """Accept a fact-check or fast-check article href, or return None.

    Relative and absolute links on www.boomlive.in (or boomlive.in) are
    both accepted. Query strings and fragments are dropped. These shapes
    match, with an optional trailing slash already stripped:

    * /fact-check/<slug>-<id>
    * /fact-check/<subcategory>/<slug>-<id>
    * /fast-check/<slug>-<id>
    * /fast-check/<subcategory>/<slug>-<id>

    <subcategory> and <slug> are [a-z0-9-]+, compared without case.
    <id> is 4–6 digits. /tags/..., /weekly-wrap/..., and any other section
    are rejected. The stored URL keeps the path casing of this href; the
    section subcategory is lowercased so Politics and politics are one value.
    fast-check is always the section "fast-check", with or without a
    subcategory. fact-check with a subcategory is "fact-check/<subcategory>".
    """
    absolute = urljoin(base_url, href.strip())
    parts = urlsplit(absolute)
    host = parts.netloc.lower().split(":")[0]
    if host == "boomlive.in":
        host = HOST
    if host != HOST:
        return None
    path = parts.path.rstrip("/")
    if not path:
        return None
    head = path.lower().strip("/").split("/", 1)[0]
    if head in {"tags", "weekly-wrap"}:
        return None
    match = ARTICLE_PATH_RE.match(path)
    if match is None:
        return None
    root = match.group("root").lower()
    subcategory = match.group("subcategory")
    if root == "fast-check":
        section = "fast-check"
    elif subcategory:
        section = f"fact-check/{subcategory.lower()}"
    else:
        section = "fact-check"
    url = urlunsplit(("https", HOST, path, "", ""))
    return ArticleLink(url=url, article_id=int(match.group("id")), section=section)


def article_id(url: str) -> int:
    """Numeric id at the end of an accepted article path. Ids increase over time."""
    parsed = parse_article_link(url, url)
    if parsed is None:
        raise CrawlAbort(f"Article URL is not a fact-check or fast-check story: {url}")
    return parsed.article_id


def section_of(url: str) -> str:
    parsed = parse_article_link(url, url)
    return parsed.section if parsed is not None else ""


def reject_disallowed_listing(url: str) -> None:
    """Hard stop if a listing URL would use the disallowed ?page= form."""
    query = urlsplit(url).query.lower()
    if query.startswith("page=") or "&page=" in f"&{query}":
        raise CrawlAbort(
            f"Refusing {url}: robots.txt disallows ?page= URLs. "
            "Listings must use /fact-check or /fact-check/N."
        )


def html_title(text: str) -> str:
    """Read <title> with lxml. A parse failure counts as no title."""
    if not text:
        return ""
    try:
        document = lxml_html.fromstring(text)
    except (lxml_html.etree.ParserError, lxml_html.etree.XMLSyntaxError, ValueError):
        return ""
    titles = document.xpath("//title/text()")
    return "".join(titles).strip()


def looks_like_block(response: requests.Response) -> bool:
    """Apply the size-and-title test. Do not look at the word "captcha".

    BOOM's real pages include that word in ordinary front-end code, so it
    is not a signal. A challenge page is small and its <title> is not the
    site's. A 404 is exempt: deleted stories and listing pages past the
    end of the archive are missing pages, not a ban, and aborting on them
    would end the crawl at the first gap. 403 and 429 are handled before
    this function is called.
    """
    if response.status_code == 404:
        return False
    if len(response.content) >= BLOCK_BODY_BYTES:
        return False
    # Case-insensitive: the brand is "BOOM", and a real title may write "Boom".
    return "BOOM" not in html_title(response.text).upper()


class PoliteClient:
    """GET with one browser UA, a 1–2s gap, and three backoff retries.

    403 and 429 raise SiteBlocked immediately. Network errors and HTTP 5xx
    are retried. Nothing else is retried, and the User-Agent never changes.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._sent_any = False

    def _pause(self, retry_index: int | None) -> None:
        """Wait before a request. The first request of the process does not.

        `retry_index` is 0, 1, or 2 for the three retries. Those waits are
        the backoff (2s, 4s, 8s). Ordinary requests wait a random 1.0–2.0s.
        A retry does not add the short sleep on top of the backoff.
        """
        if retry_index is None:
            if self._sent_any:
                time.sleep(random.uniform(*POLITE_SLEEP))
        else:
            time.sleep(RETRY_BACKOFF_SECONDS[retry_index])
        self._sent_any = True

    def get(self, url: str, *, fatal: bool) -> requests.Response | None:
        """Fetch `url`.

        `fatal=True` is for listing pages and range probes: giving up would
        leave a hole in the archive, so the run stops. `fatal=False` is for
        article pages: the URL is recorded as failed and the crawl continues.
        A block is always fatal, in either mode.
        """
        reject_disallowed_listing(url)
        last_error = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            self._pause(None if attempt == 0 else attempt - 1)
            try:
                response = self.session.get(
                    url,
                    timeout=TIMEOUT_SECONDS,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log(f"network error ({attempt + 1}/{MAX_ATTEMPTS}) {url}: {last_error}")
                continue

            if response.status_code in (403, 429):
                raise SiteBlocked(
                    f"Blocked at {response.url} (HTTP {response.status_code}). "
                    "Checkpoints already written were left in place. "
                    "This script will not retry or try to get around the block."
                )
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                log(f"server error ({attempt + 1}/{MAX_ATTEMPTS}) {url}: {last_error}")
                continue
            if looks_like_block(response):
                raise SiteBlocked(
                    f"Blocked at {response.url} "
                    "(no <title> containing BOOM, and body under 20 KB). "
                    "Checkpoints already written were left in place. "
                    "This script will not retry or try to get around the block."
                )
            return response

        message = (
            f"Gave up after 3 retries on {url} ({last_error}). "
            "Checkpoints already written were left in place."
        )
        if fatal:
            raise CrawlAbort(message)
        log(message)
        return None


# ---------------------------------------------------------------------------
# HTML extraction. BeautifulSoup uses the lxml parser. The block-title check
# above uses lxml directly so a challenge page can be judged without pulling
# article text out of it.
# ---------------------------------------------------------------------------

def soup_from(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "lxml")


def meta_content(soup: BeautifulSoup, *, prop: str = "", name: str = "") -> str:
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
    else:
        tag = soup.find("meta", attrs={"name": name})
    if tag is None:
        return ""
    return str(tag.get("content") or "").strip()


def iter_jsonld(soup: BeautifulSoup):
    """Yield each JSON-LD object, including items inside @graph and lists."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _walk_jsonld(payload)


def _walk_jsonld(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_jsonld(item)
        return
    if not isinstance(payload, dict):
        return
    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            yield from _walk_jsonld(item)
        # A node can carry both @graph and its own @type. Fall through.
    yield payload


def _types(node: dict) -> set[str]:
    raw = node.get("@type", "")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str)}
    return set()


def is_news_article(node: dict) -> bool:
    types = _types(node)
    return bool(types & {"NewsArticle", "Article"})


def select_article_node(soup: BeautifulSoup) -> dict | None:
    """Prefer a NewsArticle/Article that actually has an articleBody."""
    nodes = [node for node in iter_jsonld(soup) if is_news_article(node)]
    for node in nodes:
        body = node.get("articleBody")
        if isinstance(body, str) and body.strip():
            return node
    return nodes[0] if nodes else None


def to_ymd(value) -> str:
    """Take the calendar date as the site wrote it, as YYYY-MM-DD.

    article:published_time is an ISO timestamp with a zone, often +05:30.
    Converting that to UTC would move late-evening India stories onto the
    previous day. The date prefix is the publication date we want. A value
    that is not a real calendar day (month 13, and so on) is rejected.
    """
    if not isinstance(value, str):
        return ""
    match = YMD_RE.search(value.strip())
    if not match:
        return ""
    text = match.group(1)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def author_names(value) -> str:
    """JSON-LD author can be a string, a Person, or a list of either."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        names = [author_names(item) for item in value]
        return ", ".join(name for name in names if name)
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            return " ".join(name.split())
    return ""


def clean_text(value: str) -> str:
    """Strip each line and drop blank lines. Internal spaces collapse."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def jsonld_body(node: dict | None) -> str:
    if node is None:
        return ""
    body = node.get("articleBody")
    if not isinstance(body, str) or not body.strip():
        return ""
    # Some CMSes put HTML inside articleBody. Flatten that to text.
    if "<" in body and ">" in body:
        body = soup_from(body).get_text("\n", strip=True)
    return clean_text(body)


def paragraph_fallback(soup: BeautifulSoup) -> str:
    """Join <p> text from the main article container.

    The container is whichever of <article>, [itemprop=articleBody], and
    <main> yields the most paragraph text. The whole document is not used:
    that would mix the story with the nav, footer, and related-story blurbs.
    """
    candidates = list(soup.find_all("article"))
    by_itemprop = soup.find(attrs={"itemprop": "articleBody"})
    if by_itemprop is not None:
        candidates.append(by_itemprop)
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None:
        candidates.append(main)

    best = ""
    for container in candidates:
        parts: list[str] = []
        for paragraph in container.find_all("p"):
            text = " ".join(paragraph.get_text(" ", strip=True).split())
            if text:
                parts.append(text)
        joined = "\n".join(parts).strip()
        if len(joined) > len(best):
            best = joined
    return best


def extract_title(soup: BeautifulSoup) -> str:
    """og:title without a trailing " | BOOM", otherwise the first h1."""
    title = meta_content(soup, prop="og:title")
    title = TITLE_SUFFIX_RE.sub("", title).strip()
    if title:
        return title
    heading = soup.find("h1")
    if heading is None:
        return ""
    return " ".join(heading.get_text(" ", strip=True).split())


def extract_date(soup: BeautifulSoup, article_node: dict | None) -> str:
    """Meta article:published_time, then JSON-LD datePublished."""
    from_meta = to_ymd(meta_content(soup, prop="article:published_time"))
    if from_meta:
        return from_meta
    if article_node is None:
        return ""
    return to_ymd(article_node.get("datePublished"))


def extract_author(soup: BeautifulSoup, article_node: dict | None) -> str:
    if article_node is not None:
        name = author_names(article_node.get("author"))
        if name:
            return name
    return author_names(meta_content(soup, name="author"))


def extract_article(url: str, html: str) -> ArticleRecord:
    """Pull the four fields. Status is ok only when content is non-empty."""
    soup = soup_from(html)
    article_node = select_article_node(soup)
    content = jsonld_body(article_node) or paragraph_fallback(soup)
    record = ArticleRecord(
        url=url,
        title=extract_title(soup),
        date=extract_date(soup, article_node),
        author=extract_author(soup, article_node),
        content=content,
        section=section_of(url),
        scrape_status="ok" if content else "empty",
        error="" if content else "no content",
    )
    return record


def article_links(html: str, page_url: str) -> list[str]:
    """Article URLs in document order, one per numeric id.

    The same story can be linked as /fact-check/<slug>-<id> and as
    /fact-check/<subcategory>/<slug>-<id>. The first href on the page is
    kept. Sidebar links are still included; callers remove them later.
    Only parse_article_link decides what counts, so validity, the sidebar
    set, and URL collection all see subcategory and fast-check stories.
    """
    soup = soup_from(html)
    found: list[str] = []
    seen_ids: set[int] = set()
    for anchor in soup.find_all("a", href=True):
        parsed = parse_article_link(str(anchor["href"]), page_url)
        if parsed is None or parsed.article_id in seen_ids:
            continue
        seen_ids.add(parsed.article_id)
        found.append(parsed.url)
    return found


# ---------------------------------------------------------------------------
# Step A. Every listing page repeats the same latest/trending sidebar
# (about five links, including the newest story). Pages past the archive
# return only that sidebar, so a page is real only when it still has at
# least five other links. Those links are whatever parse_article_link
# accepts, including /fact-check/<subcategory>/... and /fast-check/...,
# so an older page is not treated as empty just because its stories are
# not /fact-check/<slug>-<id>. The sidebar is the ids shared by pages 1, 2,
# and 3. Page 1 keeps those links when URLs are collected; later pages do
# not. A page's date is the published date of its non-sidebar link with
# the lowest id, which is the oldest story on that page.
# ---------------------------------------------------------------------------

def fetch_listing_links(client: PoliteClient, page: int) -> list[str]:
    """Article links on one listing page. A 404 is an empty page, not a block."""
    page_url = listing_url(page)
    response = client.get(page_url, fatal=True)
    if response is None or response.status_code == 404:
        status = "no response" if response is None else "HTTP 404"
        log(f"[range] page {page} is {status}")
        return []
    if response.status_code != 200:
        raise CrawlAbort(
            f"Listing page {page} returned HTTP {response.status_code} ({page_url})."
        )
    return article_links(response.text, page_url)


class ListingIndex:
    """Cached listing pages, the sidebar id set, and one date per page.

    Listing HTML is fetched once. Dating a page then fetches a single
    article. That article is not written to the progress file; step C
    scrapes it again so every saved row goes through the same extractor.
    """

    def __init__(self, client: PoliteClient) -> None:
        self.client = client
        self.sidebar_ids: set[int] = set()
        self._links: dict[int, list[str]] = {}
        self._dates: dict[int, PageProbe] = {}
        self.probes: list[dict] = []

    def links(self, page: int) -> list[str]:
        if page not in self._links:
            self._links[page] = fetch_listing_links(self.client, page)
        return self._links[page]

    def non_sidebar(self, page: int) -> list[str]:
        return [url for url in self.links(page) if article_id(url) not in self.sidebar_ids]

    def is_valid(self, page: int) -> bool:
        return len(self.non_sidebar(page)) >= MIN_NON_SIDEBAR_LINKS

    def discover_sidebar(self) -> None:
        """Sidebar = ids that appear on page 1 and on both page 2 and page 3.

        Matched by id, not by the full path. The same sidebar story can be
        linked with and without a subcategory, and those are one article.
        """
        for page in (1, 2, 3):
            found = self.links(page)
            log(f"[range] page {page} has {len(found)} article link(s) before the sidebar filter")
        shared = (
            {article_id(url) for url in self._links[1]}
            & {article_id(url) for url in self._links[2]}
            & {article_id(url) for url in self._links[3]}
        )
        self.sidebar_ids = shared
        if not self.sidebar_ids:
            raise CrawlAbort(
                "The sidebar set is empty. It is the article ids shared by "
                "pages 1, 2, and 3. Without those ids, a page past the archive "
                "(which repeats only those links) cannot be told from a real page."
            )
        for url in self._links[1]:
            if article_id(url) in shared:
                log(f"[range] sidebar {article_id(url)} {url}")
        if len(self.sidebar_ids) != 5:
            log(
                f"WARNING: expected 5 sidebar links shared by pages 1, 2, and 3; "
                f"found {len(self.sidebar_ids)}. Continuing with that set."
            )
        for page in (1, 2, 3):
            count = len(self.non_sidebar(page))
            if count < MIN_NON_SIDEBAR_LINKS:
                raise CrawlAbort(
                    f"Page {page} has {count} links after removing the sidebar; "
                    f"a real page needs at least {MIN_NON_SIDEBAR_LINKS}. "
                    "The sidebar filter may be removing the article list."
                )

    def probe_validity(self, page: int) -> bool:
        """Log one validity probe. Does not fetch an article."""
        cached = page in self._links
        count = len(self.non_sidebar(page))
        valid = count >= MIN_NON_SIDEBAR_LINKS
        cached_note = " (listing cached)" if cached else ""
        log(f"[range] probe page {page} valid={valid} non_sidebar={count}{cached_note}")
        self.probes.append(
            {
                "kind": "validity",
                "page": page,
                "valid": valid,
                "non_sidebar_count": count,
                "cached_listing": cached,
            }
        )
        return valid

    def date_of(self, page: int) -> PageProbe:
        """Date the non-sidebar article with the lowest id. Log the probe."""
        cached = self._dates.get(page)
        if cached is not None:
            log(
                f"[range] probe page {page} cached date {cached.date} "
                f"id {cached.article_id}"
            )
            self.probes.append({**cached.as_dict(), "kind": "date", "cached": True})
            return cached

        candidates = self.non_sidebar(page)
        if not candidates:
            raise CrawlAbort(
                f"Page {page} has no non-sidebar article to date. "
                "The range search will not guess a side."
            )
        article_url = min(candidates, key=article_id)
        aid = article_id(article_url)
        log(f"[range] probe page {page} dating lowest id {aid} {article_url}")
        article = self.client.get(article_url, fatal=True)
        if article is None or article.status_code != 200:
            status = "no response" if article is None else f"HTTP {article.status_code}"
            raise CrawlAbort(
                f"Article {article_url} (lowest id on page {page}) returned {status}. "
                "The range search will not guess a side."
            )
        soup = soup_from(article.text)
        date = extract_date(soup, select_article_node(soup))
        if not date:
            raise CrawlAbort(
                f"No publication date for {article_url} (lowest id on page {page}). "
                "The range search will not guess a side."
            )
        probed = PageProbe(
            page=page,
            listing_url=listing_url(page),
            article_url=article_url,
            article_id=aid,
            date=date,
            valid=len(candidates) >= MIN_NON_SIDEBAR_LINKS,
            non_sidebar_count=len(candidates),
        )
        self._dates[page] = probed
        log(f"[range] page {page} date {date} (id {aid})")
        self.probes.append({**probed.as_dict(), "kind": "date", "cached": False})
        return probed


def find_last_valid_page(index: ListingIndex) -> int:
    """Highest page in 1..2000 that still has a real article list.

    Valid pages are a prefix: page 1 is real, and once a page is only the
    sidebar every higher page is too. The search walks toward that edge.
    Counts come from parse_article_link, so /fact-check/<subcategory>/...
    and /fast-check/... keep a page valid. The older slug-only pattern
    dropped those and could stop the archive near page 168.
    """
    low = SEARCH_FIRST_PAGE
    high = SEARCH_LAST_PAGE
    best: int | None = None
    while low <= high:
        mid = (low + high) // 2
        if index.probe_validity(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    if best is None:
        raise CrawlAbort(
            f"No valid listing page in {SEARCH_FIRST_PAGE}–{SEARCH_LAST_PAGE}. "
            f"A valid page has at least {MIN_NON_SIDEBAR_LINKS} links that are "
            "not in the sidebar."
        )
    log(f"[range] last valid page is {best}")
    return best


def find_highest_page_on_or_after(index: ListingIndex, high_page: int, threshold: str) -> int:
    """Rightmost page in 1..high_page whose oldest real article is >= threshold.

    Higher page numbers are older, so a date still inside the window means
    the boundary is at this page or further down the archive.
    """
    low = SEARCH_FIRST_PAGE
    high = high_page
    best: int | None = None
    while low <= high:
        mid = (low + high) // 2
        probed = index.date_of(mid)
        if probed.date >= threshold:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    if best is None:
        raise CrawlAbort(
            f"No listing page in {SEARCH_FIRST_PAGE}–{high_page} has a lowest-id "
            f"article dated on or after {threshold}."
        )
    return best


def find_lowest_page_on_or_before(index: ListingIndex, high_page: int, threshold: str) -> int:
    """Leftmost page in 1..high_page whose oldest real article is <= threshold.

    Pages before this one are newer than the end of 2025 (2026, on this
    site). The crawl buffer may still touch a couple of them.
    """
    low = SEARCH_FIRST_PAGE
    high = high_page
    best: int | None = None
    while low <= high:
        mid = (low + high) // 2
        probed = index.date_of(mid)
        if probed.date <= threshold:
            best = mid
            high = mid - 1
        else:
            low = mid + 1
    if best is None:
        raise CrawlAbort(
            f"No listing page in {SEARCH_FIRST_PAGE}–{high_page} has a lowest-id "
            f"article dated on or before {threshold}."
        )
    return best


def find_page_range(client: PoliteClient) -> dict:
    """Last valid page, then the 2022 and 2025 edges inside that prefix."""
    index = ListingIndex(client)
    index.discover_sidebar()
    last_valid = find_last_valid_page(index)
    earliest = index.date_of(last_valid)
    reaches_2022 = earliest.date < WINDOW_START
    if reaches_2022:
        end_page = find_highest_page_on_or_after(index, last_valid, WINDOW_START)
    else:
        # The oldest real listing page is still inside 2022–or-later, so
        # there is no older page to search for. Keep the whole valid archive
        # and say so. This is not a block and it does not stop the crawl.
        end_page = last_valid
        log(
            f"WARNING: the last valid BOOM listing page is {last_valid} and its "
            f"oldest real article is dated {earliest.date}, which is still on or "
            f"after {WINDOW_START}. The listing does not reach 2022. "
            f"end_page is {last_valid}. Earliest reachable date: {earliest.date}."
        )
    start_page = find_lowest_page_on_or_before(index, end_page, WINDOW_END)
    if start_page > end_page:
        raise CrawlAbort(
            f"Range search produced start_page {start_page} after end_page {end_page}."
        )
    crawl_start = max(SEARCH_FIRST_PAGE, start_page - PAGE_BUFFER)
    crawl_end = min(last_valid, end_page + PAGE_BUFFER)
    # Already probed above. Reading the cache avoids a second "probe" line.
    start_date = index._dates[start_page].date
    end_date = index._dates[end_page].date
    log(
        f"[range] start page {start_page} ({start_date}), "
        f"end page {end_page} ({end_date}), "
        f"last valid page {last_valid} ({earliest.date}), "
        f"crawl {crawl_start}–{crawl_end}"
    )
    return {
        "start_page": start_page,
        "end_page": end_page,
        "last_valid_page": last_valid,
        "earliest_reachable_date": earliest.date,
        "sidebar_ids": sorted(index.sidebar_ids),
        "link_filter": LINK_FILTER,
        "start_page_date": start_date,
        "end_page_date": end_date,
        "reaches_2022": reaches_2022,
        "buffer_pages": PAGE_BUFFER,
        "crawl_start": crawl_start,
        "crawl_end": crawl_end,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "probes": index.probes,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_page_range(path: Path) -> dict:
    """Reuse a saved range. The ±2 buffer is recomputed and clamped.

    The clamp uses last_valid_page so a skipped search cannot walk into
    pages that are only the sidebar. sidebar_ids are required: an older
    range file from before the sidebar filter cannot drive URL collection.
    """
    if not path.is_file():
        raise CrawlAbort(
            f"Cannot skip the range search: {path} does not exist. "
            "Run once without --skip-range-search."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CrawlAbort(f"{path} is not valid JSON: {exc}") from exc
    if payload.get("link_filter") != LINK_FILTER:
        raise CrawlAbort(
            f"{path} used a different article-link filter. Subcategory and "
            "fast-check URLs were dropped, so pages past about 168 can now be "
            "valid and the range has to be searched again. "
            "Re-run without --skip-range-search."
        )
    try:
        start_page = int(payload["start_page"])
        end_page = int(payload["end_page"])
        last_valid = int(payload["last_valid_page"])
        sidebar_ids = [int(item) for item in payload["sidebar_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise CrawlAbort(
            f"{path} must contain start_page, end_page, last_valid_page, and "
            "sidebar_ids. Re-run without --skip-range-search."
        ) from exc
    if start_page < 1 or end_page < start_page or last_valid < end_page:
        raise CrawlAbort(
            f"{path} has an unusable range start_page={start_page} "
            f"end_page={end_page} last_valid_page={last_valid}."
        )
    if not sidebar_ids:
        raise CrawlAbort(
            f"{path} has an empty sidebar_ids list. Re-run without --skip-range-search."
        )
    payload["sidebar_ids"] = sidebar_ids
    payload["buffer_pages"] = PAGE_BUFFER
    payload["crawl_start"] = max(1, start_page - PAGE_BUFFER)
    payload["crawl_end"] = min(last_valid, end_page + PAGE_BUFFER)
    log(
        f"[range] reused {path}: pages {start_page}–{end_page}, "
        f"last valid {last_valid}, "
        f"crawl {payload['crawl_start']}–{payload['crawl_end']}, "
        f"{len(sidebar_ids)} sidebar id(s)"
    )
    return payload


def sidebar_id_set(page_range: dict) -> set[int]:
    """Ids saved with the range. Missing means the file predates the filter."""
    raw = page_range.get("sidebar_ids")
    if not isinstance(raw, list) or not raw:
        raise CrawlAbort(
            "The page range has no sidebar ids, so listing links cannot be "
            "filtered. Re-run without --skip-range-search."
        )
    return {int(item) for item in raw}


def links_to_keep(page: int, links: list[str], sidebar_ids: set[int]) -> list[str]:
    """Page 1 keeps every link. Later pages drop the sidebar.

    The sidebar stories are the newest ones and already appear on page 1.
    Leaving them on later pages would store the same URLs on every page
    and, past the archive, would store nothing else.
    """
    if page == 1:
        return links
    return [url for url in links if article_id(url) not in sidebar_ids]


# ---------------------------------------------------------------------------
# Step B. One CSV row per article URL seen on a listing page. A page that
# yields nothing still gets a row with a blank url, so resume can see that
# the page was crawled. The file is appended and fsynced after every page;
# rewriting it each time could truncate the checkpoint if the process dies
# mid-write.
# ---------------------------------------------------------------------------

def load_crawled_pages(path: Path) -> tuple[set[int], list[tuple[int, str]]]:
    """Return pages already checkpointed, and (page, url) rows in file order."""
    if not path.is_file():
        return set(), []
    pages: set[int] = set()
    rows: list[tuple[int, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            if row is None:
                continue
            raw_page = (row.get("listing_page") or "").strip()
            url = (row.get("url") or "").strip()
            if not raw_page:
                log(f"Skipping {path} line {line_number}: no listing_page.")
                continue
            try:
                page = int(raw_page)
            except ValueError:
                log(f"Skipping {path} line {line_number}: bad listing_page {raw_page!r}.")
                continue
            pages.add(page)
            rows.append((page, url))
    return pages, rows


def _append_url_rows(path: Path, rows: list[tuple[int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.is_file() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(URL_COLUMNS)
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def url_checkpoint_uses_current_filter() -> bool:
    """True when boom_urls.csv was collected with the current link matcher.

    No CSV yet counts as current: there is nothing old to distrust. A CSV
    without this stamp was built when subcategory and fast-check links were
    dropped, so those listing pages have to be read again.
    """
    if not URLS_PATH.is_file():
        return True
    if not URL_FILTER_STAMP.is_file():
        return False
    return URL_FILTER_STAMP.read_text(encoding="utf-8").strip() == LINK_FILTER


def stamp_url_filter_if_complete(crawl_start: int, crawl_end: int) -> None:
    """Record the matcher only after the whole window is in the CSV.

    Writing the stamp earlier would let a later run skip pages that were
    only collected with the old regex.
    """
    pages, _rows = load_crawled_pages(URLS_PATH)
    needed = set(range(crawl_start, crawl_end + 1))
    if not needed or not needed <= pages:
        return
    URL_FILTER_STAMP.parent.mkdir(parents=True, exist_ok=True)
    URL_FILTER_STAMP.write_text(LINK_FILTER + "\n", encoding="utf-8")


def collect_urls(client: PoliteClient, page_range: dict, max_pages: int | None) -> None:
    crawl_start = int(page_range["crawl_start"])
    crawl_end = int(page_range["crawl_end"])
    sidebar_ids = sidebar_id_set(page_range)
    if url_checkpoint_uses_current_filter():
        done_pages, _existing = load_crawled_pages(URLS_PATH)
    else:
        done_pages = set()
        log(
            "[urls] boom_urls.csv was collected before subcategory and "
            "fast-check links were accepted. Those listing pages will be "
            "read again. Articles already scraped are still skipped by id."
        )
    # A page is appended only after its full link list is in memory, then
    # fsynced. The last page in the file is finished, so resume continues
    # at the next page that is not in the file. Gaps are filled too.
    pending = [page for page in range(crawl_start, crawl_end + 1) if page not in done_pages]
    if max_pages is not None:
        # Testing cap: the first N pages of this run, not of the whole archive.
        # Already checkpointed pages are not counted against the cap.
        pending = pending[:max_pages]
        log(f"[urls] --max-pages {max_pages}: crawling {len(pending)} listing page(s)")
    if not pending:
        log("[urls] every listing page in the range is already in the checkpoint")
        stamp_url_filter_if_complete(crawl_start, crawl_end)
        return

    log(
        f"[urls] {len(done_pages)} page(s) already checkpointed; "
        f"{len(pending)} left in {crawl_start}–{crawl_end}"
    )
    for page in tqdm(pending, desc="BOOM listing pages", unit="page"):
        page_url = listing_url(page)
        response = client.get(page_url, fatal=True)
        if response is None:
            raise CrawlAbort(
                f"Listing page {page} ({page_url}) was not fetched. "
                "Progress saved through the previous page."
            )
        if response.status_code == 404:
            links = []
            log(f"[urls] page {page} is HTTP 404")
        elif response.status_code != 200:
            raise CrawlAbort(
                f"Listing page {page} returned HTTP {response.status_code} ({page_url}). "
                "Progress saved through the previous page."
            )
        else:
            raw_links = article_links(response.text, page_url)
            links = links_to_keep(page, raw_links, sidebar_ids)
            if not links:
                log(f"[urls] page {page} has no fact-check article links after the sidebar filter")
        # A blank url marks the page done when it contributed no stories.
        rows = [(page, url) for url in links] or [(page, "")]
        _append_url_rows(URLS_PATH, rows)
        if response.status_code == 200:
            ignored = len(raw_links) - len(links)
            if page == 1:
                log(f"[urls] page 1: {len(links)} article link(s), sidebar kept")
            else:
                log(
                    f"[urls] page {page}: {len(links)} article link(s), "
                    f"{ignored} sidebar link(s) ignored"
                )
    stamp_url_filter_if_complete(crawl_start, crawl_end)


def urls_to_scrape(page_range: dict) -> list[str]:
    """Unique articles inside the current crawl window, one URL per id.

    Listing order is page number, then the order the links were saved.
    The first path wins when the same id appears as both
    /fact-check/<slug>-<id> and /fact-check/<subcategory>/<slug>-<id>.
    Sidebar ids are dropped on every page after page 1. A URL that does
    not match parse_article_link is skipped rather than scraped.
    """
    crawl_start = int(page_range["crawl_start"])
    crawl_end = int(page_range["crawl_end"])
    sidebar_ids = sidebar_id_set(page_range)
    _pages, rows = load_crawled_pages(URLS_PATH)
    ordered: list[str] = []
    seen_ids: set[int] = set()
    for page, url in sorted(rows, key=lambda item: item[0]):
        if not url or not (crawl_start <= page <= crawl_end):
            continue
        parsed = parse_article_link(url, url)
        if parsed is None:
            continue
        if page != 1 and parsed.article_id in sidebar_ids:
            continue
        if parsed.article_id in seen_ids:
            continue
        seen_ids.add(parsed.article_id)
        ordered.append(parsed.url)
    return ordered


# ---------------------------------------------------------------------------
# Step C. The progress file is the resume key: any URL already present is
# skipped, including failed and empty ones. Delete those lines to retry them.
# ---------------------------------------------------------------------------

def load_done_urls(path: Path) -> tuple[set[str], set[int]]:
    """URLs and ids already in the progress file.

    The id set is what skips a second path for the same story. The URL set
    still skips an exact href that was saved before this matcher existed.
    """
    if not path.is_file():
        return set(), set()
    done_urls: set[str] = set()
    done_ids: set[int] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                log(f"Skipping unreadable progress line {line_number}.")
                continue
            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not url:
                log(f"Skipping progress line {line_number} with no url.")
                continue
            done_urls.add(url)
            parsed = parse_article_link(url, url)
            if parsed is not None:
                done_ids.add(parsed.article_id)
    return done_urls, done_ids


def append_progress(path: Path, records: list[ArticleRecord]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def scrape_one(client: PoliteClient, url: str) -> ArticleRecord:
    response = client.get(url, fatal=False)
    if response is None:
        return ArticleRecord(
            url=url,
            section=section_of(url),
            scrape_status="failed",
            error="retries exhausted",
        )
    if response.status_code != 200:
        return ArticleRecord(
            url=url,
            section=section_of(url),
            scrape_status="failed",
            error=f"HTTP {response.status_code}",
        )
    return extract_article(url, response.text)


def _already_scraped(url: str, done_urls: set[str], done_ids: set[int]) -> bool:
    if url in done_urls:
        return True
    parsed = parse_article_link(url, url)
    return parsed is not None and parsed.article_id in done_ids


def scrape_articles(client: PoliteClient, urls: list[str], max_articles: int | None) -> None:
    done_urls, done_ids = load_done_urls(PROGRESS_PATH)
    pending = [url for url in urls if not _already_scraped(url, done_urls, done_ids)]
    already = len(urls) - len(pending)
    if max_articles is not None:
        pending = pending[:max_articles]
        bar_total = len(pending)
        bar_initial = 0
        log(
            f"[articles] {already} already done; "
            f"--max-articles {max_articles} scrapes {len(pending)} more"
        )
    else:
        bar_total = len(urls)
        bar_initial = already
        log(f"[articles] {already} already done; scraping {len(pending)}")
    if not pending:
        log("[articles] every collected URL is already in the progress file")
        return

    batch: list[ArticleRecord] = []
    progress = tqdm(
        pending,
        total=bar_total,
        initial=bar_initial,
        desc="BOOM articles",
        unit="article",
    )
    try:
        for url in progress:
            batch.append(scrape_one(client, url))
            if len(batch) >= ARTICLE_CHECKPOINT_EVERY:
                append_progress(PROGRESS_PATH, batch)
                batch.clear()
    finally:
        # A block, a retry abort, or Ctrl+C still keeps the open batch.
        progress.close()
        append_progress(PROGRESS_PATH, batch)
        batch.clear()


# ---------------------------------------------------------------------------
# Step D. Year files are rebuilt from the progress file, so a resumed run
# replaces them with whatever is checkpointed. Caps (--max-pages,
# --max-articles) therefore produce partial CSVs on purpose.
# ---------------------------------------------------------------------------

def load_progress(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                log(f"Skipping unreadable progress line {line_number}.")
                continue
            if isinstance(payload, dict) and payload.get("url"):
                records.append(payload)
    return records


def _record_key(record: dict) -> str:
    """Dedupe key. The same numeric id is one article even if the path differs."""
    url = str(record.get("url") or "")
    parsed = parse_article_link(url, url)
    if parsed is not None:
        return f"id:{parsed.article_id}"
    return f"url:{url}"


def dedupe_records(records: list[dict]) -> list[dict]:
    """One row per numeric id. The first URL seen is the one that stays.

    A later line replaces that row only when the first scrape has no body
    and the later one does. The first URL and section are kept even then.
    """
    chosen: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        url = str(record.get("url") or "")
        if not url:
            continue
        key = _record_key(record)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = record
            order.append(key)
            continue
        previous_body = str(previous.get("content") or "").strip()
        new_body = str(record.get("content") or "").strip()
        if new_body and not previous_body:
            kept = dict(record)
            kept["url"] = previous.get("url") or url
            if previous.get("section"):
                kept["section"] = previous["section"]
            chosen[key] = kept
    return [chosen[key] for key in order]


def write_year_files(records: list[dict]) -> tuple[dict[str, int], int]:
    """Write boom_<year>.csv. Return per-year counts and the failed/empty count."""
    unique = dedupe_records(records)
    failed_or_empty = sum(
        1 for record in unique if record.get("scrape_status") in {"failed", "empty"}
    )
    kept_by_year: dict[str, list[dict]] = {str(year): [] for year in range(2022, 2026)}
    outside = 0
    for record in unique:
        if record.get("scrape_status") != "ok":
            continue
        content = str(record.get("content") or "").strip()
        date = str(record.get("date") or "").strip()
        if not content:
            continue
        if not (WINDOW_START <= date <= WINDOW_END):
            outside += 1
            continue
        year = date[:4]
        if year not in kept_by_year:
            outside += 1
            continue
        url = str(record.get("url") or "")
        kept_by_year[year].append(
            {
                "title": record.get("title") or "",
                "url": url,
                "date": date,
                "author": record.get("author") or "",
                "content": content,
                "section": record.get("section") or section_of(url),
            }
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for year, rows in kept_by_year.items():
        frame = pd.DataFrame(rows, columns=YEAR_COLUMNS)
        destination = OUTPUT_DIR / f"boom_{year}.csv"
        temporary = destination.with_suffix(".csv.tmp")
        frame.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(destination)
        counts[year] = len(frame)
    if outside:
        log(f"[output] {outside} scraped article(s) outside {WINDOW_START}..{WINDOW_END}")
    return counts, failed_or_empty


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect BOOM English fact-checks from 2022-01-01 through 2025-12-31. "
            "Listing pages use /fact-check/N only."
        )
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help=(
            "Testing cap. Crawl at most this many not-yet-checkpointed listing "
            "pages after the range is known. Does not shrink the binary search."
        ),
    )
    parser.add_argument(
        "--max-articles",
        type=_positive_int,
        default=None,
        help=(
            "Testing cap. Scrape at most this many articles that are not already "
            "in the progress file."
        ),
    )
    parser.add_argument(
        "--skip-range-search",
        action="store_true",
        help=f"Reuse {RANGE_PATH.name} instead of probing listing pages.",
    )
    return parser.parse_args(argv)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    client = PoliteClient()
    try:
        if args.skip_range_search:
            page_range = load_page_range(RANGE_PATH)
        else:
            page_range = find_page_range(client)
            write_json(RANGE_PATH, page_range)
            log(f"[range] wrote {RANGE_PATH}")
        collect_urls(client, page_range, args.max_pages)
        scrape_articles(client, urls_to_scrape(page_range), args.max_articles)
        counts, failed_or_empty = write_year_files(load_progress(PROGRESS_PATH))
    except CrawlAbort as exc:
        # SiteBlocked is a CrawlAbort. Article batches flush in a finally
        # before this handler runs. Listing rows are already fsynced per page.
        sys.exit(str(exc))

    for year in ("2022", "2023", "2024", "2025"):
        print(f"{year}: {counts.get(year, 0)}")
    print(f"failed or empty: {failed_or_empty}")


if __name__ == "__main__":
    main()
