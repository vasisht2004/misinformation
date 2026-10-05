#!/usr/bin/env python3
"""Per-site and cross-site analysis of classified fact-check CSVs.

The script builds the tables, charts, and markdown report for a study of
health misinformation in Indian fact-checking (2022–2025). It reads the
CSVs produced by classify.py (prompt v3) and applies the study definitions
below. It does not recode articles and it does not drop rows.

Study definitions (applied exactly):

* Year comes from the filename token ``_full_<year>_``. The scrapes were
  split by year before classification, so the filename year is authoritative.
  The ``date`` column is parsed only to count disagreements with that year.
* The fact-check denominator is rows with ``is_fact_check`` true. When a
  file has no ``is_fact_check`` column, every row in that file stays in the
  denominator. Total rows and fact-check rows are both reported.
* Health rows are fact-checks with ``health_related`` true.
* Health misinformation is the health subset whose ``rating_normalized`` is
  False, Misleading, or Unproven. A health article rated True stays in the
  health denominator and is outside the misinformation count.
* Headline health counts are computed twice: "All claims", and
  "Excl. FC international product series". That second view drops only
  Fact Crescendo rows whose ``claim_region`` is International. Every other
  site keeps all of its rows, because a spot-check found International
  unreliable outside that series.

Usage (paths are resolved from the current working directory)::

    python analyze.py
    python analyze.py --inputs "data/classified/*_full_*_classified.csv" \\
        --outdir analysis

Requires pandas and matplotlib. The default glob is not recursive, so
``data/classified/v2_backup/`` is left out.
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import matplotlib

# Save PNGs without opening a window. Must be set before pyplot is imported.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import MaxNLocator

import pandas as pd


# Years the report covers. Files whose filename year falls outside this
# window are described in the data notes and left out of every total.
YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025)

# A site-year below this many fact-checks is "low n". Those years are left
# out of trend statements. Direction is described only when the share moves
# by at least one percentage point; a smaller move is "roughly flat".
LOW_N_FACT_CHECKS = 100
TREND_PP = 1.0

# A percentage whose denominator is below this is written with its count
# ("19.3%, 11 of 57"). The site section then says "small base" once.
SMALL_BASE = 50

REGION_CAVEAT = (
    "A manual spot-check found claim_region unreliable outside Fact Crescendo's "
    "international consumer-product series, so only that series is excluded in "
    "the adjusted view."
)

# Pooled deeper findings are computed three times. Charts use the view that
# drops Fact Crescendo's international product series, because those checks
# otherwise dominate the pool. View ids are the CSV values.
VIEW_SPECS = (
    ("all_claims", "All claims, all sites"),
    ("excl_fc_intl", "Excl. FC international product series"),
    ("exclude_factcrescendo", "Excluding Fact Crescendo"),
)
CHART_VIEW = "excl_fc_intl"
CHART_VIEW_LABEL = "Excl. FC international product series"

# Health misinformation, per the protocol. True stays in the health
# denominator and is deliberately absent from this set.
MISINFO_RATINGS = {"False", "Misleading", "Unproven"}

# Summary column "pct_false_or_misleading": Unproven is not part of this
# narrower share. The misinformation count above still includes Unproven.
FALSE_OR_MISLEADING = {"False", "Misleading"}

# Verdicts drawn left-to-right in the ratings chart. Labels outside this
# list are appended afterwards, sorted alphabetically.
RATING_ORDER = ("False", "Misleading", "Unproven", "True", "Satire", "Not stated")

# Protocol region labels. Anything else is reported in the data notes.
KNOWN_REGIONS = {"India", "International", "Unclear"}

# Display names used in titles, tables, and the draft write-ups.
# Unknown site ids keep the raw ``site`` value.
DISPLAY_NAMES = {
    "indiatoday": "India Today",
    "pti": "PTI",
    "factcrescendo": "Fact Crescendo",
    "boom": "BOOM",
}

# Stable site order in cross-site charts. Sites not listed here follow,
# sorted by display name.
PREFERRED_SITES = ("boom", "factcrescendo", "indiatoday", "pti")

# Columns whose absence must not crash a metric. Older exports may lack
# some of these. A missing column skips that metric and is listed in the
# data notes.
BOOL_COLUMNS = ("is_fact_check", "health_related", "ai_generated_misinfo")
STRING_COLUMNS = (
    "health_topic",
    "rating_normalized",
    "platforms",
    "verification_methods",
    "evidence_disclosed",
    "claim_region",
)
TRACKED_COLUMNS = (
    "date",
    "site",
    "is_fact_check",
    "health_related",
    "health_topic",
    "rating_normalized",
    "platforms",
    "verification_methods",
    "evidence_disclosed",
    "ai_generated_misinfo",
    "claim_region",
)

# Yes/No is how classify.py writes booleans. True/False is accepted so a
# hand-edited or older file still normalises.
_BOOL_TRUE = {"yes", "true"}
_BOOL_FALSE = {"no", "false"}

# Token comparison for verification methods is case-insensitive. The
# protocol strings are "Expert consultation" and "Scientific literature".
EXPERT_TOKEN = "Expert consultation"
LITERATURE_TOKEN = "Scientific literature"
LINKED_SOURCES = "linked sources"

SUMMARY_COLUMNS = (
    "site",
    "fact_checks",
    "health",
    "health_share_pct",
    "health_misinfo",
    "health_excl_fc_intl",
    "health_share_excl_fc_intl_pct",
    "top_topic",
    "top_topic_pct",
    "pct_false_or_misleading",
    "pct_ai_generated",
    "pct_international",
)

# Chart style. One teal is the study colour; the amber is the second
# series (the adjusted Fact Crescendo view, non-health, or the share line).
MAIN = "#0E7C86"
SECOND = "#E09F3E"
ACCENT = "#9B2226"
PALETTE = (
    "#0E7C86",
    "#E09F3E",
    "#3D5A80",
    "#9B2226",
    "#6A4C93",
    "#2A9D8F",
    "#BB3E03",
    "#005F73",
    "#94D2BD",
    "#AE2012",
    "#CA6702",
    "#0A9396",
    "#EE9B00",
    "#001219",
)
DPI = 150
YEAR_RE = re.compile(r"_full_(\d{4})_")
SITE_RE = re.compile(r"^(.+)_full_\d{4}_")

# Metric labels for the health vs non-health comparison, in chart order.
HVNH_METRICS = (
    ("pct_false", "False"),
    ("pct_misleading", "Misleading"),
    ("pct_ai_generated", "AI-generated"),
    ("pct_expert_consultation", "Expert consultation"),
    ("pct_scientific_literature", "Scientific literature"),
    ("pct_linked_sources", "Linked sources"),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def display_name(site: str) -> str:
    """Map a site id to the name used in the paper.

    Lookup is case-insensitive. An id that is not in the map is returned
    unchanged so a newly added outlet still appears under its own name.
    """
    key = site.strip().lower()
    return DISPLAY_NAMES.get(key, site.strip())


def site_dirname(site: str) -> str:
    """Folder name for one outlet's charts. Raw ids are kept when safe."""
    cleaned = site.strip()
    if re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        return cleaned
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("_")
    return slug or "unknown"


def infer_site(filename: str) -> str:
    """Site id from ``<site>_full_<year>_...`` when the column is absent."""
    match = SITE_RE.match(filename)
    if match:
        return match.group(1)
    return "unknown"


def order_sites(sites: list[str]) -> list[str]:
    """Preferred study order, then any other site ids by display name."""
    present = set(sites)
    ordered = [site for site in PREFERRED_SITES if site in present]
    rest = [site for site in sites if site not in PREFERRED_SITES]
    rest.sort(key=lambda site: (display_name(site).casefold(), site))
    return ordered + rest


def share_pct(numer: int, denom: int) -> float | None:
    """Percentage rounded half-up to one decimal place.

    One decimal is the reporting precision used in every table and sentence.
    Decimal arithmetic avoids binary-float noise and Python's banker's round,
    so a value of x.x5 always rounds away from zero.
    """
    if denom <= 0:
        return None
    quant = (Decimal(numer) * Decimal(100)) / Decimal(denom)
    return float(quant.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def cell_str(value: object) -> str:
    """Strip a cell to text. Missing values become an empty string."""
    if value is None or value is pd.NA:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def split_tokens(value: object) -> list[str]:
    """Split a ``;``-joined cell. Whitespace around each token is removed.

    classify.py joins lists with ``"; "``. Splitting on ``;`` and stripping
    keeps both that form and a form with no space after the semicolon.
    Empty pieces (a trailing semicolon, a blank cell) contribute nothing.
    """
    text = cell_str(value)
    if not text:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def token_counts(series: pd.Series) -> list[tuple[str, int]]:
    """Count tokens across cells. Each token counts once per row.

    A repeated platform inside one cell ("WhatsApp; WhatsApp") is one
    article, not two. Sort is count descending, then name, so a tie has a
    stable order.
    """
    counts: Counter[str] = Counter()
    for value in series.tolist():
        counts.update(set(split_tokens(value)))
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))


def contains_token(series: pd.Series, token: str) -> pd.Series:
    """True when ``token`` is one of the semicolon-separated pieces.

    Comparison is case-insensitive so a capitalisation difference does not
    zero out the expert-consultation or scientific-literature shares.
    """
    wanted = token.casefold()
    flags = []
    for value in series.tolist():
        flags.append(any(part.casefold() == wanted for part in split_tokens(value)))
    return pd.Series(flags, index=series.index, dtype=bool)


def normalise_bool(series: pd.Series) -> tuple[pd.Series, int]:
    """Map Yes/No/True/False (any casing) to pandas BooleanDtype.

    Any other cell, including a blank, stays missing and is counted in the
    returned integer. Callers must not treat that missing value as False:
    an unrecognised ``is_fact_check`` is left out of the denominator, and an
    unrecognised ``health_related`` is in neither the health nor the
    non-health group.
    """
    values: list[object] = []
    unknown = 0
    for raw in series.tolist():
        token = cell_str(raw).casefold()
        if token in _BOOL_TRUE:
            values.append(True)
        elif token in _BOOL_FALSE:
            values.append(False)
        else:
            values.append(pd.NA)
            unknown += 1
    normalised = pd.Series(pd.array(values, dtype="boolean"), index=series.index)
    return normalised, unknown


def as_bool_true(series: pd.Series) -> pd.Series:
    """Plain True/False mask that is True only for boolean True.

    Comparing a nullable boolean to True yields ``<NA>`` on missing cells,
    and ``<NA>`` cannot be used as a row filter. ``pd.NA is True`` is False,
    so missing cells collapse to False without turning an explicit False
    into missing.
    """
    return pd.Series(
        [value is True for value in series.tolist()],
        index=series.index,
        dtype=bool,
    )


def as_bool_false(series: pd.Series) -> pd.Series:
    """Plain True/False mask that is True only for boolean False."""
    return pd.Series(
        [value is False for value in series.tolist()],
        index=series.index,
        dtype=bool,
    )


def label_or_blank(value: object) -> str:
    """Use the coded label, or ``(blank)`` when the cell is empty."""
    text = cell_str(value)
    return text if text else "(blank)"


def fmt_int(value: int | None) -> str:
    """Thousands-separated integer, or ``n/a`` when the metric was skipped."""
    if value is None:
        return "n/a"
    return f"{value:,}"


def fmt_pct(value: float | None) -> str:
    """One-decimal percentage with a % sign, or ``n/a``."""
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


class ShareStyle:
    """Formats percentages and remembers when a base was below ``SMALL_BASE``.

    The site section uses that flag to add the words "small base" once,
    rather than on every small percentage.
    """

    def __init__(self) -> None:
        self.small = False

    def pct(self, value: float | None, numer: int | None, denom: int | None) -> str:
        if value is None:
            return "n/a"
        if denom is not None and numer is not None and denom < SMALL_BASE:
            self.small = True
            return f"{value:.1f}%, {numer:,} of {denom:,}"
        return f"{value:.1f}%"


def small_base_note(style: ShareStyle) -> str:
    """One trailing sentence for a site section that used a small denominator."""
    if not style.small:
        return ""
    return " Small base."


def is_fact_crescendo(site: str) -> bool:
    """True for the Fact Crescendo site id or its display name."""
    key = site.strip().casefold()
    return key in {"factcrescendo", "fact crescendo"}


def view_frame(df: pd.DataFrame, view: str) -> pd.DataFrame:
    """Row slice for one sensitivity view.

    ``excl_fc_intl`` drops only Fact Crescendo rows coded International.
    Every other site keeps all of its rows, including rows coded
    International. Excluding Fact Crescendo drops that outlet entirely.
    All-claims keeps every study-window row.
    """
    if view == "all_claims":
        return df
    if view == "excl_fc_intl":
        return df.loc[df["in_excl_fc_intl"]].copy()
    if view == "exclude_factcrescendo":
        keep = ~df["site"].map(lambda value: is_fact_crescendo(str(value)))
        return df.loc[keep].copy()
    raise KeyError(view)


def view_label(view: str) -> str:
    for view_id, label in VIEW_SPECS:
        if view_id == view:
            return label
    return view


def low_n_years(site: SiteStats) -> list[YearStats]:
    """Site-years that have rows but fewer than 100 fact-checks."""
    return [
        year
        for year in site.years
        if year.rows > 0 and year.fact_checks < LOW_N_FACT_CHECKS
    ]


def low_n_exclusion_sentence(site: SiteStats) -> str:
    """The required exclusion sentence, or empty when every year is large enough."""
    excluded = low_n_years(site)
    if not excluded:
        return ""
    if len(excluded) == 1:
        year = excluded[0]
        return (
            f" {site.display} {year.year} has only {year.fact_checks:,} fact-checks "
            "and is excluded from trend calculations."
        )
    bits = [f"{site.display} {year.year} ({year.fact_checks:,} fact-checks)" for year in excluded]
    return " " + join_and(bits) + " are excluded from trend calculations."


def share_move(start: float, end: float) -> str:
    """'rises', 'falls', or 'is roughly flat' from a one-decimal share change.

    The threshold is one percentage point on the reported (already rounded)
    percentages, so the verb matches the figures in the sentence.
    """
    delta = float(
        (Decimal(str(end)) - Decimal(str(start))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    )
    if delta >= TREND_PP:
        return "rises"
    if delta <= -TREND_PP:
        return "falls"
    return "is roughly flat"


def year_table_label(stats: YearStats) -> str:
    """Year label for the site table, with a low-n flag when it applies."""
    if stats.year is None:
        return "Total"
    if stats.rows > 0 and stats.fact_checks < LOW_N_FACT_CHECKS:
        return f"{stats.year} (low n)"
    return str(stats.year)


def dominant_rows(df: pd.DataFrame, mask: pd.Series, noun: str) -> str:
    """Which site contributes the most rows in ``mask``, with its share.

    Example: "132 of 152 health-related fact-checks are from Fact Crescendo".
    """
    part = df.loc[mask]
    total = int(len(part))
    if total <= 0:
        return f"no {noun} are in this view"
    counts = part["site"].map(lambda value: display_name(str(value))).value_counts()
    top = int(counts.max())
    names = sorted(str(name) for name, count in counts.items() if int(count) == top)
    if len(names) == 1:
        return f"{top:,} of {total:,} {noun} are from {names[0]}"
    return f"{top:,} of {total:,} {noun} are tied between {join_and(names)}"


def stability_sentence(signatures: dict[str, object], what: str) -> str:
    """Say whether a headline result is shared by all three views.

    ``signatures`` maps a view id to a comparable value (a tuple or a string).
    A difference from the all-claims signature is attributed to Fact Crescendo
    when the exclude-Fact-Crescendo view is the one that moves, and to that
    outlet's international product series when the adjusted view is the one
    that moves.
    """
    ordered = [signatures[view_id] for view_id, _label in VIEW_SPECS if view_id in signatures]
    if len(ordered) < 2:
        return f"This {what} is available for only one view."
    if all(item == ordered[0] for item in ordered[1:]):
        return f"This {what} holds across all three views."
    bits: list[str] = []
    if "exclude_factcrescendo" in signatures and signatures["exclude_factcrescendo"] != signatures.get("all_claims"):
        bits.append("it changes when Fact Crescendo is excluded, so it is driven by Fact Crescendo")
    if "excl_fc_intl" in signatures and signatures["excl_fc_intl"] != signatures.get("all_claims"):
        bits.append(
            "it changes when Fact Crescendo's international product series is excluded, "
            "so that series affects it"
        )
    if not bits:
        bits.append("the three views do not agree")
    return f"This {what} does not hold across views: " + "; ".join(bits) + "."


def fmt_pp(delta: float) -> str:
    """Signed percentage-point difference, already on the one-decimal scale."""
    rounded = float(Decimal(str(delta)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return f"{rounded:+.1f} percentage points"


def join_and(items: list[str]) -> str:
    """English list: 'A', 'A and B', or 'A, B, and C'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def md_cell(value: object) -> str:
    """Make a value safe inside a markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    """Render a GitHub-style table."""
    head = "| " + " | ".join(md_cell(header) for header in headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(md_cell(cell) for cell in row) + " |"
        for row in rows
    ]
    return "\n".join([head, rule, *body])


def finite_numbers(values: list[object]) -> list[float]:
    """Drop None and NaN so axis limits are computed from real bar heights."""
    numbers: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            numbers.append(number)
    return numbers


def positive_limit(values: list[object], scale: float = 1.22, floor: float = 1.0) -> float:
    """Upper axis limit with room for value labels. Never zero."""
    numbers = finite_numbers(values)
    peak = max(numbers) if numbers else 0.0
    return max(peak * scale, floor)


def leaders_at_max(pairs: list[tuple[str, float]]) -> list[str]:
    """Names that share the highest value. Empty if every value is missing."""
    usable = [(name, value) for name, value in pairs if value is not None]
    if not usable:
        return []
    top = max(value for _, value in usable)
    return sorted(name for name, value in usable if value == top)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class FileNote:
    """Data-quality record for one input CSV. Rendered in the report."""

    file: str
    rows: int = 0
    year: int | None = None
    skipped: str | None = None
    missing_columns: list[str] = field(default_factory=list)
    date_mismatch: int | None = None
    date_unparsed: int | None = None
    bool_unrecognised: dict[str, int] = field(default_factory=dict)
    fact_checks: int | None = None
    site_fill: str | None = None
    duplicate_flags: int | None = None
    status_not_ok: int | None = None
    status_blank: int | None = None
    prompt_versions: dict[str, int] = field(default_factory=dict)


def _prepare_frame(path: Path, year: int, note: FileNote) -> pd.DataFrame:
    """Read one CSV, attach year/site, and normalise types.

    Every tracked column is created even when the file lacks it, together
    with a ``has_<column>`` flag. Later metrics consult the flag. A blank
    string in a column the file never had is then distinguishable from a
    blank the coder left empty.
    """
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    note.rows = int(len(frame))
    note.year = year
    note.missing_columns = [column for column in TRACKED_COLUMNS if column not in frame.columns]

    # Site id: prefer the column. Fill blanks, or the whole column, from
    # the filename so one bad cell does not create an empty-site group.
    inferred = infer_site(path.name)
    if "site" not in frame.columns:
        frame["site"] = inferred
        note.site_fill = f"site column missing; set to {inferred} from the filename"
    else:
        frame["site"] = frame["site"].map(cell_str)
        blank = frame["site"] == ""
        n_blank = int(blank.sum())
        if n_blank:
            frame.loc[blank, "site"] = inferred
            note.site_fill = f"{n_blank} blank site values filled with {inferred} from the filename"

    frame["year"] = year
    frame["source_file"] = path.name

    # Date is not used as the analysis year. It is parsed only so the
    # report can say how often the article date and the filename year disagree.
    if "date" in frame.columns:
        parsed = pd.to_datetime(
            frame["date"],
            format="mixed",
            dayfirst=True,
            errors="coerce",
        )
        parsed_year = parsed.dt.year
        # NaT compared with != is True in pandas, so the notna mask is required.
        mismatch = parsed_year.notna() & (parsed_year != year)
        note.date_mismatch = int(mismatch.sum())
        note.date_unparsed = int(parsed_year.isna().sum())
    else:
        note.date_mismatch = None
        note.date_unparsed = None

    for column in BOOL_COLUMNS:
        flag = f"has_{column}"
        if column not in frame.columns:
            frame[column] = pd.Series(pd.NA, index=frame.index, dtype="boolean")
            frame[flag] = False
        else:
            frame[column], n_unknown = normalise_bool(frame[column])
            frame[flag] = True
            if n_unknown:
                note.bool_unrecognised[column] = n_unknown

    for column in STRING_COLUMNS:
        flag = f"has_{column}"
        if column not in frame.columns:
            frame[column] = ""
            frame[flag] = False
        else:
            frame[column] = frame[column].map(cell_str)
            frame[flag] = True

    if "possible_duplicate_title" in frame.columns:
        flags, _ = normalise_bool(frame["possible_duplicate_title"])
        note.duplicate_flags = int(as_bool_true(flags).sum())
    else:
        note.duplicate_flags = None

    if "status" in frame.columns:
        status = frame["status"].map(cell_str)
        note.status_blank = int((status == "").sum())
        note.status_not_ok = int(((status != "") & (status.str.casefold() != "ok")).sum())
    else:
        note.status_blank = None
        note.status_not_ok = None

    if "prompt_version" in frame.columns:
        versions = frame["prompt_version"].map(cell_str)
        note.prompt_versions = {
            (label if label else "(blank)"): int(count)
            for label, count in versions.value_counts().items()
        }
    return frame


def load_inputs(pattern: str) -> tuple[pd.DataFrame, list[FileNote]]:
    """Read every CSV matched by ``pattern`` and stack them.

    ``pattern`` may be absolute or relative to the current working directory.
    A file whose name has no ``_full_<year>_`` token is skipped and noted.
    """
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise SystemExit(f"No files matched {pattern!r}")

    frames: list[pd.DataFrame] = []
    notes: list[FileNote] = []
    for path_str in paths:
        path = Path(path_str)
        note = FileNote(file=path.name)
        year_match = YEAR_RE.search(path.name)
        if not year_match:
            note.skipped = "filename has no _full_<year>_ token; file was not analysed"
            notes.append(note)
            continue
        year = int(year_match.group(1))
        frames.append(_prepare_frame(path, year, note))
        notes.append(note)

    if not frames:
        raise SystemExit("No classified CSVs had a _full_<year>_ token in the filename.")

    data = pd.concat(frames, ignore_index=True, sort=False)
    return data, notes


@dataclass
class AppliedCorrection:
    """One corrections.csv row that matched exactly one article."""

    site: str
    title_contains: str
    field: str
    new_value: str
    reason: str
    matched_title: str
    topic_reset: bool


def _write_correction_value(df: pd.DataFrame, idx: object, field: str, new_value: str) -> None:
    """Write ``new_value`` into one cell, mapping Yes/No on boolean columns.

    Boolean columns are stored as pandas BooleanDtype. ``as_bool_true`` only
    accepts a real True, so a correction of No must be stored as False rather
    than the string "No".
    """
    if field in BOOL_COLUMNS:
        token = new_value.casefold()
        if token in _BOOL_TRUE:
            df.at[idx, field] = True
        elif token in _BOOL_FALSE:
            df.at[idx, field] = False
        else:
            df.at[idx, field] = pd.NA
        return
    df.at[idx, field] = new_value


def apply_corrections(df: pd.DataFrame, path: Path) -> tuple[list[AppliedCorrection], bool]:
    """Apply manual label fixes before any metric is computed.

    Each corrections.csv row must match exactly one record: the ``site``
    column equals ``site`` and ``title`` contains ``title_contains``,
    case-insensitive. Zero matches or several matches print a warning and
    leave those rows unchanged. Setting ``health_related`` to No also sets
    ``health_topic`` to "Not health-related".

    Returns the applied corrections and whether the file was present.
    """
    if not path.is_file():
        return [], False

    table = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = ["site", "title_contains", "field", "new_value", "reason"]
    missing = [column for column in required if column not in table.columns]
    if missing:
        print(f"Warning: {path} is missing columns {', '.join(missing)}; no corrections applied.")
        return [], True
    if "title" not in df.columns or "site" not in df.columns:
        print("Warning: the classified data has no title or site column; every correction was skipped.")
        return [], True

    titles = df["title"].map(lambda value: "" if pd.isna(value) else str(value))
    sites = df["site"].map(cell_str)
    applied: list[AppliedCorrection] = []
    for record in table.itertuples(index=False):
        site = cell_str(record.site)
        needle = cell_str(record.title_contains)
        field = cell_str(record.field)
        new_value = cell_str(record.new_value)
        reason = cell_str(record.reason)
        if not site or not needle or not field:
            print(
                f"Warning: correction site={site!r} title_contains={needle!r} "
                f"field={field!r} is incomplete; skipped."
            )
            continue
        if field not in df.columns:
            print(f"Warning: correction field {field!r} is not in the data; skipped.")
            continue
        mask = sites.str.casefold().eq(site.casefold()) & titles.str.casefold().str.contains(
            needle.casefold(),
            regex=False,
        )
        hits = df.index[mask]
        n_hits = int(len(hits))
        if n_hits != 1:
            print(
                f"Warning: correction site={site!r} title_contains={needle!r} "
                f"matched {n_hits} records; skipped."
            )
            continue
        idx = hits[0]
        _write_correction_value(df, idx, field, new_value)
        topic_reset = False
        if field == "health_related" and new_value.casefold() in _BOOL_FALSE and "health_topic" in df.columns:
            df.at[idx, "health_topic"] = "Not health-related"
            topic_reset = True
        applied.append(
            AppliedCorrection(
                site=site,
                title_contains=needle,
                field=field,
                new_value=new_value,
                reason=reason,
                matched_title=titles.at[idx],
                topic_reset=topic_reset,
            )
        )
    return applied, True


def add_masks(df: pd.DataFrame) -> None:
    """Attach the fact-check, health, and non-health row masks.

    Fact-check rule: a row counts when ``is_fact_check`` is true, or when
    the source file had no such column (the flag is false for every row of
    that file). An unrecognised value in a file that does have the column
    does not count.

    Health rule: fact-check and ``health_related`` true.
    Non-health rule: fact-check and ``health_related`` false.
    Unrecognised ``health_related`` is in neither group.
    """
    for column in list(df.columns):
        if column.startswith("has_"):
            df[column] = df[column].astype(bool)

    fact_true = as_bool_true(df["is_fact_check"])
    df["row_is_fact"] = (~df["has_is_fact_check"]) | fact_true

    health_true = as_bool_true(df["health_related"])
    health_false = as_bool_false(df["health_related"])
    df["row_is_health"] = df["row_is_fact"] & df["has_health_related"] & health_true
    df["row_is_nonhealth"] = df["row_is_fact"] & df["has_health_related"] & health_false


def add_indicators(df: pd.DataFrame) -> None:
    """Precompute the yes/no fields the metrics share.

    Indicators are False when the underlying cell does not match. The
    ``has_*`` flags still decide whether a row enters a percentage's
    denominator, so a file that lacked the column is not scored as a string
    of negative answers.
    """
    rating = df["rating_normalized"].map(cell_str)
    df["rating_normalized"] = rating
    df["rating_is_false"] = rating.eq("False")
    df["rating_is_misleading"] = rating.eq("Misleading")
    df["rating_is_misinfo"] = rating.isin(MISINFO_RATINGS)
    df["rating_is_false_or_misleading"] = rating.isin(FALSE_OR_MISLEADING)
    df["rating_is_true"] = rating.eq("True")

    region = df["claim_region"].map(cell_str)
    df["claim_region"] = region
    df["region_international"] = region.eq("International")
    # Adjusted view: drop Fact Crescendo rows coded International, and keep
    # every other row. International on BOOM, India Today, and PTI stays in.
    fact_crescendo = df["site"].map(lambda value: is_fact_crescendo(cell_str(value)))
    df["in_excl_fc_intl"] = ~(fact_crescendo & df["region_international"])

    df["ai_true"] = as_bool_true(df["ai_generated_misinfo"])
    df["token_expert"] = contains_token(df["verification_methods"], EXPERT_TOKEN)
    df["token_literature"] = contains_token(df["verification_methods"], LITERATURE_TOKEN)
    df["is_linked_sources"] = df["evidence_disclosed"].map(
        lambda value: cell_str(value).casefold() == LINKED_SOURCES
    )


def attach_fact_counts(notes: list[FileNote], df: pd.DataFrame) -> None:
    """Record in-window fact-check rows on each file note.

    The date-mismatch counts were taken on the whole file. The fact-check
    count here is restricted to 2022–2025 so the notes reconcile with the
    tables. A file from another year contributes zero study fact-checks.
    """
    in_window = df["year"].isin(YEARS)
    for note in notes:
        if note.skipped or note.year not in YEARS:
            if note.fact_checks is None and not note.skipped:
                note.fact_checks = 0
            continue
        part = df.loc[in_window & (df["source_file"] == note.file), "row_is_fact"]
        note.fact_checks = int(part.sum())


# ---------------------------------------------------------------------------
# Per-site statistics
# ---------------------------------------------------------------------------


@dataclass
class Availability:
    """Which coding columns this site has on at least one row."""

    health: bool
    rating: bool
    region: bool
    topic: bool
    platforms: bool
    methods: bool
    evidence: bool
    ai: bool


@dataclass
class YearStats:
    """Headline counts for one year, or for the site total (``year is None``)."""

    year: int | None
    rows: int
    fact_checks: int
    health: int | None
    health_share_pct: float | None
    health_misinfo: int | None
    fact_checks_excl_fc_intl: int | None
    health_excl_fc_intl: int | None
    health_share_excl_fc_intl_pct: float | None
    health_misinfo_excl_fc_intl: int | None


@dataclass
class SiteStats:
    """Everything the site section of the report needs."""

    site: str
    display: str
    dirname: str
    availability: Availability
    years: list[YearStats]
    total: YearStats
    topics: list[tuple[str, int, float]]
    ratings: list[tuple[str, int, float]]
    platforms: list[tuple[str, int, float]]
    methods: list[tuple[str, int, float]]
    evidence: list[tuple[str, int]]
    ai_n: int | None
    ai_denom: int | None
    ai_pct: float | None
    intl_n: int | None
    intl_denom: int | None
    intl_pct: float | None
    false_or_misleading_n: int | None
    false_or_misleading_denom: int | None
    false_or_misleading_pct: float | None
    true_n: int | None
    caveats: list[str]


def _availability(df: pd.DataFrame) -> Availability:
    """True when at least one row of this site came from a file with the column."""
    def has(column: str) -> bool:
        flag = f"has_{column}"
        return bool(df[flag].any()) if len(df) else False

    return Availability(
        health=has("health_related"),
        rating=has("rating_normalized"),
        region=has("claim_region"),
        topic=has("health_topic"),
        platforms=has("platforms"),
        methods=has("verification_methods"),
        evidence=has("evidence_disclosed"),
        ai=has("ai_generated_misinfo"),
    )


def _slice_stats(df: pd.DataFrame, year: int | None, caps: Availability) -> YearStats:
    """Headline counts for a row slice.

    ``caps`` is the site-level availability. A year with no rows still
    returns zeros for a metric the site can measure, and None for a metric
    the site cannot measure at all. That keeps "measured zero" distinct from
    "column missing".
    """
    fact = df["row_is_fact"]
    health = df["row_is_health"]
    n_rows = int(len(df))
    n_fact = int(fact.sum())
    n_health = int(health.sum()) if caps.health else None
    health_share = share_pct(n_health, n_fact) if n_health is not None else None

    if caps.rating and caps.health:
        # Rows that lack rating_normalized do not add to the count. The
        # site caveat says when that coverage is incomplete.
        misinfo = int((health & df["has_rating_normalized"] & df["rating_is_misinfo"]).sum())
    else:
        misinfo = None

    # The adjusted view is defined for every site. It only removes Fact
    # Crescendo rows coded International, so other sites match all claims.
    adjusted_fact_mask = fact & df["in_excl_fc_intl"]
    n_adjusted_fact = int(adjusted_fact_mask.sum())
    if caps.health:
        adjusted_health_mask = health & df["in_excl_fc_intl"]
        n_adjusted_health = int(adjusted_health_mask.sum())
        adjusted_share = share_pct(n_adjusted_health, n_adjusted_fact)
        if caps.rating:
            adjusted_misinfo = int(
                (adjusted_health_mask & df["has_rating_normalized"] & df["rating_is_misinfo"]).sum()
            )
        else:
            adjusted_misinfo = None
    else:
        n_adjusted_health = None
        adjusted_share = None
        adjusted_misinfo = None

    return YearStats(
        year=year,
        rows=n_rows,
        fact_checks=n_fact,
        health=n_health,
        health_share_pct=health_share,
        health_misinfo=misinfo,
        fact_checks_excl_fc_intl=n_adjusted_fact,
        health_excl_fc_intl=n_adjusted_health,
        health_share_excl_fc_intl_pct=adjusted_share,
        health_misinfo_excl_fc_intl=adjusted_misinfo,
    )


def _distribution(
    labels: pd.Series,
    denom: int,
    order: tuple[str, ...] | None = None,
) -> list[tuple[str, int, float]]:
    """(label, count, percent of denom), largest count first unless ``order`` is set."""
    if denom <= 0:
        return []
    counts = labels.map(label_or_blank).value_counts()
    items = [(str(label), int(count), share_pct(int(count), denom) or 0.0) for label, count in counts.items()]
    if order is None:
        items.sort(key=lambda item: (-item[1], item[0].casefold()))
        return items
    rank = {name: index for index, name in enumerate(order)}
    items.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0].casefold()))
    return items


def _site_caveats(df: pd.DataFrame, caps: Availability) -> list[str]:
    """Short reading notes for incomplete coding on this site. Only non-zero issues."""
    notes: list[str] = []
    fact = df["row_is_fact"]
    health = df["row_is_health"]
    n_fact = int(fact.sum())
    n_health = int(health.sum())

    # Rows from a file with no is_fact_check column are kept in the denominator.
    n_missing_fc_rows = int((~df["has_is_fact_check"]).sum())
    if n_missing_fc_rows:
        notes.append(
            f"is_fact_check is missing for {n_missing_fc_rows:,} rows; "
            "those rows are included in the fact-check denominator."
        )

    if not caps.health:
        notes.append("health_related is missing; health metrics were skipped for this site.")
    else:
        uncoded = int((fact & df["has_health_related"] & ~health & ~df["row_is_nonhealth"]).sum())
        # Unrecognised boolean: column present, value neither True nor False.
        if uncoded:
            notes.append(
                f"{uncoded:,} fact-checks have an unrecognised health_related value "
                "and are in neither the health nor the non-health group."
            )
        missing_health_rows = int((fact & ~df["has_health_related"]).sum())
        if missing_health_rows:
            notes.append(
                f"health_related is missing for {missing_health_rows:,} fact-checks; "
                "those rows are in neither group."
            )

    if caps.health and n_health and not caps.rating:
        notes.append("rating_normalized is missing; verdict and health-misinformation figures were skipped.")
    elif caps.health and caps.rating and n_health:
        missing_rating = int((health & ~df["has_rating_normalized"]).sum())
        if missing_rating:
            notes.append(
                f"rating_normalized is missing for {missing_rating:,} health-related rows; "
                "verdict shares use only the rows that have the column."
            )

    if not caps.region:
        notes.append("claim_region is missing; the international share was skipped.")
    elif n_fact:
        missing_region = int((fact & ~df["has_claim_region"]).sum())
        if missing_region:
            notes.append(
                f"claim_region is missing for {missing_region:,} fact-checks; "
                "the international share omits them. A missing region is not the code "
                "International, so those rows stay in the adjusted view."
            )
        blank_region = int((fact & df["has_claim_region"] & df["claim_region"].eq("")).sum())
        if blank_region:
            notes.append(
                f"{blank_region:,} fact-checks have a blank claim_region. "
                "A blank is not the code International, so those rows stay in the adjusted view."
            )

    if caps.health and n_health and caps.topic:
        missing_topic = int((health & ~df["has_health_topic"]).sum())
        if missing_topic:
            notes.append(
                f"health_topic is missing for {missing_topic:,} health-related rows; "
                "the topic distribution omits them."
            )
    elif caps.health and n_health and not caps.topic:
        notes.append("health_topic is missing; the topic distribution was skipped.")

    if caps.health and n_health and not caps.platforms:
        notes.append("platforms is missing; platform counts were skipped.")
    if caps.health and n_health and not caps.methods:
        notes.append("verification_methods is missing; verification-method counts were skipped.")
    if caps.health and n_health and not caps.evidence:
        notes.append("evidence_disclosed is missing; evidence counts were skipped.")
    if caps.health and n_health and not caps.ai:
        notes.append("ai_generated_misinfo is missing; the AI-generated share was skipped.")
    elif caps.health and caps.ai and n_health:
        missing_ai = int((health & ~df["has_ai_generated_misinfo"]).sum())
        if missing_ai:
            notes.append(
                f"ai_generated_misinfo is missing for {missing_ai:,} health-related rows; "
                "the AI share uses only the rows that have the column."
            )

    return notes


def compute_site(df: pd.DataFrame, site: str) -> SiteStats:
    """All per-site metrics for 2022–2025, including the adjusted region view."""
    caps = _availability(df)
    years = [_slice_stats(df.loc[df["year"] == year], year, caps) for year in YEARS]
    total = _slice_stats(df, None, caps)

    health_rows = df.loc[df["row_is_health"]]
    n_health = int(len(health_rows))

    topics: list[tuple[str, int, float]] = []
    if caps.topic and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_health_topic"]]
        topics = _distribution(coded["health_topic"], int(len(coded)))

    ratings: list[tuple[str, int, float]] = []
    false_n: int | None = None
    false_denom: int | None = None
    false_pct: float | None = None
    true_n: int | None = None
    if caps.rating and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_rating_normalized"]]
        false_denom = int(len(coded))
        ratings = _distribution(coded["rating_normalized"], false_denom, RATING_ORDER)
        false_n = int(coded["rating_is_false_or_misleading"].sum()) if false_denom else 0
        false_pct = share_pct(false_n, false_denom)
        true_n = int(coded["rating_is_true"].sum()) if false_denom else 0
    elif not caps.rating or not caps.health:
        false_n = None
        false_denom = None
        false_pct = None
        true_n = None

    platforms: list[tuple[str, int, float]] = []
    if caps.platforms and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_platforms"]]
        denom = int(len(coded))
        platforms = [
            (name, count, share_pct(count, denom) or 0.0)
            for name, count in token_counts(coded["platforms"])
        ]

    methods: list[tuple[str, int, float]] = []
    if caps.methods and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_verification_methods"]]
        denom = int(len(coded))
        methods = [
            (name, count, share_pct(count, denom) or 0.0)
            for name, count in token_counts(coded["verification_methods"])
        ]

    evidence: list[tuple[str, int]] = []
    if caps.evidence and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_evidence_disclosed"], "evidence_disclosed"]
        counts = coded.map(label_or_blank).value_counts()
        evidence = sorted(
            ((str(label), int(count)) for label, count in counts.items()),
            key=lambda item: (-item[1], item[0].casefold()),
        )

    ai_n: int | None = None
    ai_denom: int | None = None
    ai_pct: float | None = None
    if caps.ai and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_ai_generated_misinfo"]]
        ai_denom = int(len(coded))
        ai_n = int(coded["ai_true"].sum()) if ai_denom else 0
        ai_pct = share_pct(ai_n, ai_denom)

    intl_n: int | None = None
    intl_denom: int | None = None
    intl_pct: float | None = None
    if caps.region and caps.health and n_health:
        coded = health_rows.loc[health_rows["has_claim_region"]]
        intl_denom = int(len(coded))
        intl_n = int(coded["region_international"].sum()) if intl_denom else 0
        intl_pct = share_pct(intl_n, intl_denom)

    return SiteStats(
        site=site,
        display=display_name(site),
        dirname=site_dirname(site),
        availability=caps,
        years=years,
        total=total,
        topics=topics,
        ratings=ratings,
        platforms=platforms,
        methods=methods,
        evidence=evidence,
        ai_n=ai_n,
        ai_denom=ai_denom,
        ai_pct=ai_pct,
        intl_n=intl_n,
        intl_denom=intl_denom,
        intl_pct=intl_pct,
        false_or_misleading_n=false_n,
        false_or_misleading_denom=false_denom,
        false_or_misleading_pct=false_pct,
        true_n=true_n,
        caveats=_site_caveats(df, caps),
    )


def top_topic_fields(site: SiteStats) -> tuple[str | None, float | None]:
    """Summary-table topic cells. Ties share one cell, joined with ' / '."""
    if not site.topics:
        return None, None
    top_count = max(count for _, count, _ in site.topics)
    leaders = [name for name, count, _ in site.topics if count == top_count]
    # Every leader has the same percentage because the denominator is shared.
    pct = next(pct for name, count, pct in site.topics if count == top_count)
    label = leaders[0] if len(leaders) == 1 else " / ".join(sorted(leaders, key=str.casefold))
    return label, pct


def summary_records(sites: list[SiteStats]) -> list[dict[str, object]]:
    """One row per site for summary_table.csv. Display names go in ``site``."""
    records: list[dict[str, object]] = []
    for site in sites:
        topic, topic_pct = top_topic_fields(site)
        total = site.total
        records.append(
            {
                "site": site.display,
                "fact_checks": total.fact_checks,
                "health": total.health,
                "health_share_pct": total.health_share_pct,
                "health_misinfo": total.health_misinfo,
                "health_excl_fc_intl": total.health_excl_fc_intl,
                "health_share_excl_fc_intl_pct": total.health_share_excl_fc_intl_pct,
                "top_topic": topic,
                "top_topic_pct": topic_pct,
                "pct_false_or_misleading": site.false_or_misleading_pct,
                "pct_ai_generated": site.ai_pct,
                "pct_international": site.intl_pct,
            }
        )
    return records


def write_table(frame: pd.DataFrame, path: Path) -> None:
    """Write a CSV. Missing metrics are blank cells, not the string 'nan'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def summary_frame(records: list[dict[str, object]]) -> pd.DataFrame:
    """Summary table with counts as integers-or-blank and percents to 1 decimal."""
    count_cols = {"fact_checks", "health", "health_misinfo", "health_excl_fc_intl"}
    pct_cols = {
        "health_share_pct",
        "health_share_excl_fc_intl_pct",
        "top_topic_pct",
        "pct_false_or_misleading",
        "pct_ai_generated",
        "pct_international",
    }
    rows: list[dict[str, object]] = []
    for record in records:
        row: dict[str, object] = {}
        for column in SUMMARY_COLUMNS:
            value = record[column]
            if value is None:
                row[column] = ""
            elif column in count_cols:
                row[column] = str(int(value))
            elif column in pct_cols:
                row[column] = f"{float(value):.1f}"
            else:
                row[column] = str(value)
        rows.append(row)
    return pd.DataFrame(rows, columns=list(SUMMARY_COLUMNS))


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _apply_rcparams() -> None:
    """Readable defaults. White figure, dark text, no extra chart chrome."""
    plt.rcParams.update(
        {
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "axes.edgecolor": "#333333",
            "text.color": "#1a1a1a",
            "axes.labelcolor": "#1a1a1a",
            "xtick.color": "#1a1a1a",
            "ytick.color": "#1a1a1a",
            "axes.unicode_minus": False,
        }
    )


def _new_fig(width: float, height: float) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(width, height))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    return fig, ax


def _style_axes(ax: plt.Axes, keep_right: bool = False) -> None:
    ax.spines["top"].set_visible(False)
    if not keep_right:
        ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#333333")
    ax.spines["bottom"].set_color("#333333")


def _grid(ax: plt.Axes, axis: str) -> None:
    ax.grid(True, axis=axis, color="#E6E6E6", linewidth=0.8)
    ax.set_axisbelow(True)


def _legend_below(ax: plt.Axes, handles=None, labels=None, ncol: int = 2) -> None:
    """Put the legend under the axes so it cannot cover bars or lines.

    ``bbox_inches="tight"`` on save keeps the legend in the PNG. The anchor
    sits below the x-axis label.
    """
    kwargs = dict(
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.28),
        ncol=ncol,
        borderaxespad=0.2,
    )
    if handles is None:
        ax.legend(**kwargs)
    else:
        ax.legend(handles, labels, **kwargs)


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _format_bar_value(height: float, kind: str) -> str:
    if kind == "pct":
        return f"{height:.1f}"
    return f"{int(round(height)):,}"


def _label_vertical(ax: plt.Axes, bars, kind: str) -> None:
    """Write the bar height just above each vertical bar."""
    for bar in bars:
        height = bar.get_height()
        if isinstance(height, float) and height != height:
            continue
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            height,
            _format_bar_value(height, kind),
            ha="center",
            va="bottom",
            fontsize=8,
            color="#1a1a1a",
        )


def _label_horizontal(ax: plt.Axes, bars, kind: str) -> None:
    """Write the bar length to the right of each horizontal bar.

    Zero-length bars are left unlabeled; there is no visible bar to tag,
    and the table carries the zero.
    """
    for bar in bars:
        width = bar.get_width()
        if isinstance(width, float) and width != width:
            continue
        if width <= 0:
            continue
        ax.text(
            width,
            bar.get_y() + bar.get_height() / 2,
            " " + _format_bar_value(width, kind),
            ha="left",
            va="center",
            fontsize=8,
            color="#1a1a1a",
        )


def _empty_chart(path: Path, title: str, message: str) -> None:
    """Still write the PNG so the report link resolves when a metric is empty."""
    fig, ax = _new_fig(8, 4.2)
    ax.axis("off")
    ax.set_title(title, pad=12)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=12, transform=ax.transAxes)
    _save(fig, path)


def plot_articles_by_year(site: SiteStats, path: Path) -> None:
    """Vertical bars: fact-checks per year, 2022–2025."""
    title = f"{site.display} — Fact-checks by year, 2022–2025"
    values = [year.fact_checks for year in site.years]
    fig, ax = _new_fig(8, 4.6)
    bars = ax.bar([str(year) for year in YEARS], values, color=MAIN, width=0.72, zorder=2)
    _label_vertical(ax, bars, "count")
    ax.set_ylabel("Fact-checks")
    ax.set_xlabel("Year")
    ax.set_title(title, pad=12)
    ax.set_ylim(0, positive_limit(values))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    _grid(ax, "y")
    _style_axes(ax)
    _save(fig, path)


def plot_health_by_year(site: SiteStats, path: Path) -> None:
    """Bars are the health count; the line on the right axis is the health share.

    The line uses the all-claims denominator. The adjusted view, which drops
    Fact Crescendo's international product series, stays in the site table
    for that outlet so this chart has a single, named denominator.
    """
    title = f"{site.display} — Health-related fact-checks by year, 2022–2025"
    if not site.availability.health:
        _empty_chart(path, title, "health_related is missing for this site")
        return
    counts = [0 if year.health is None else year.health for year in site.years]
    shares = [year.health_share_pct for year in site.years]
    fig, ax = _new_fig(8, 4.8)
    bars = ax.bar([str(year) for year in YEARS], counts, color=MAIN, width=0.72, zorder=2, label="Health-related fact-checks")
    _label_vertical(ax, bars, "count")
    ax.set_ylabel("Health-related fact-checks")
    ax.set_xlabel("Year")
    ax.set_ylim(0, positive_limit(counts))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    _grid(ax, "y")
    _style_axes(ax, keep_right=True)

    ax2 = ax.twinx()
    share_values = [float("nan") if share is None else share for share in shares]
    (line,) = ax2.plot(
        [str(year) for year in YEARS],
        share_values,
        color=ACCENT,
        marker="o",
        linewidth=2,
        label="Health share (%)",
        zorder=3,
    )
    for year, share in zip(YEARS, shares):
        if share is None:
            continue
        ax2.annotate(
            f"{share:.1f}",
            (str(year), share),
            textcoords="offset points",
            xytext=(0, 8),
            ha="center",
            fontsize=8,
            color=ACCENT,
        )
    share_peak = max(finite_numbers(shares) or [0.0])
    ax2.set_ylim(0, min(100, max(10, share_peak * 1.45)))
    ax2.set_ylabel("Health share (%)")
    ax2.spines["top"].set_visible(False)
    ax.set_title(title, pad=12)
    _legend_below(ax, [bars, line], ["Health-related fact-checks", "Health share (%)"], ncol=2)
    _save(fig, path)


def _empty_distribution_message(site: SiteStats, column_ready: bool, column_label: str) -> str:
    """Explain an empty topic, verdict, or platform chart without calling it a zero."""
    if not site.availability.health:
        return "health_related is missing for this site"
    if site.total.health in (None, 0):
        return "No health-related fact-checks"
    if not column_ready:
        return f"{column_label} is missing for this site"
    return f"No {column_label} values on health-related fact-checks"


def plot_topics(site: SiteStats, path: Path) -> None:
    """Horizontal bars of health topics, largest at the top."""
    title = f"{site.display} — Health topics, 2022–2025"
    if not site.topics:
        _empty_chart(
            path,
            title,
            _empty_distribution_message(site, site.availability.topic, "health_topic"),
        )
        return
    # barh places the first category at the bottom, so feed the smallest first.
    ordered = list(reversed(site.topics))
    labels = [name for name, _, _ in ordered]
    values = [pct for _, _, pct in ordered]
    fig, ax = _new_fig(9, max(4.2, 0.48 * len(ordered) + 1.4))
    bars = ax.barh(labels, values, color=MAIN, zorder=2)
    _label_horizontal(ax, bars, "pct")
    ax.set_xlabel("Share of health-related fact-checks (%)")
    ax.set_title(title, pad=12)
    ax.set_xlim(0, positive_limit(values, floor=5))
    _grid(ax, "x")
    _style_axes(ax)
    _save(fig, path)


def plot_ratings(site: SiteStats, path: Path) -> None:
    """Vertical bars of normalised verdicts among health rows."""
    title = f"{site.display} — Verdicts among health-related fact-checks, 2022–2025"
    if not site.ratings:
        _empty_chart(
            path,
            title,
            _empty_distribution_message(site, site.availability.rating, "rating_normalized"),
        )
        return
    labels = [name for name, _, _ in site.ratings]
    values = [count for _, count, _ in site.ratings]
    fig, ax = _new_fig(8, 4.6)
    bars = ax.bar(labels, values, color=MAIN, width=0.72, zorder=2)
    _label_vertical(ax, bars, "count")
    ax.set_ylabel("Health-related fact-checks")
    ax.set_xlabel("Normalised verdict")
    ax.set_title(title, pad=12)
    ax.set_ylim(0, positive_limit(values))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    _grid(ax, "y")
    _style_axes(ax)
    _save(fig, path)


def plot_platforms(site: SiteStats, path: Path) -> None:
    """Top 8 platforms among health rows. The bar length is an article count."""
    title = f"{site.display} — Platforms among health-related fact-checks, 2022–2025"
    if not site.platforms:
        _empty_chart(
            path,
            title,
            _empty_distribution_message(site, site.availability.platforms, "platforms"),
        )
        return
    top = list(reversed(site.platforms[:8]))
    labels = [name for name, _, _ in top]
    values = [count for _, count, _ in top]
    fig, ax = _new_fig(9, max(4.2, 0.48 * len(top) + 1.6))
    bars = ax.barh(labels, values, color=MAIN, zorder=2)
    _label_horizontal(ax, bars, "count")
    ax.set_xlabel("Health-related fact-checks naming the platform")
    ax.set_title(title, pad=12)
    ax.set_xlim(0, positive_limit(values))
    _grid(ax, "x")
    _style_axes(ax)
    _save(fig, path)


def plot_site_charts(site: SiteStats, folder: Path) -> None:
    """Write the five per-site PNGs into ``<outdir>/<site>/``."""
    plot_articles_by_year(site, folder / "articles_by_year.png")
    plot_health_by_year(site, folder / "health_by_year.png")
    plot_topics(site, folder / "topics.png")
    plot_ratings(site, folder / "ratings.png")
    plot_platforms(site, folder / "platforms.png")


def plot_health_share_by_site(sites: list[SiteStats], path: Path) -> None:
    """Two bars per site: all-claims health share and the adjusted health share.

    The second series drops Fact Crescendo rows coded International. For
    BOOM, India Today, and PTI the two bars match, because none of their
    rows are dropped.
    """
    title = "Health share by site, 2022–2025"
    labels = [site.display for site in sites]
    # A missing share is a gap, not a bar at zero. Zero is only a measured 0%.
    all_shares = [
        float("nan") if site.total.health_share_pct is None else site.total.health_share_pct
        for site in sites
    ]
    adjusted_shares = [
        float("nan") if site.total.health_share_excl_fc_intl_pct is None else site.total.health_share_excl_fc_intl_pct
        for site in sites
    ]
    positions = list(range(len(sites)))
    width = 0.36
    fig, ax = _new_fig(max(8, 1.6 * len(sites) + 2), 5.4)
    bars_all = ax.bar(
        [pos - width / 2 for pos in positions],
        all_shares,
        width=width,
        color=MAIN,
        label="All claims",
        zorder=2,
    )
    bars_adjusted = ax.bar(
        [pos + width / 2 for pos in positions],
        adjusted_shares,
        width=width,
        color=SECOND,
        label=CHART_VIEW_LABEL,
        zorder=2,
    )
    _label_vertical(ax, bars_all, "pct")
    _label_vertical(ax, bars_adjusted, "pct")
    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Health share of fact-checks (%)")
    ax.set_title(title, pad=12)
    ax.set_ylim(0, positive_limit(all_shares + finite_numbers(adjusted_shares), floor=5))
    _legend_below(ax, ncol=2)
    _grid(ax, "y")
    _style_axes(ax)
    _save(fig, path)


def plot_health_share_by_year(sites: list[SiteStats], path: Path) -> None:
    """One line per site: all-claims health share across 2022–2025."""
    title = "Health share by year, 2022–2025"
    fig, ax = _new_fig(8.5, 5)
    plotted = False
    for index, site in enumerate(sites):
        if not site.availability.health:
            continue
        values = [float("nan") if year.health_share_pct is None else year.health_share_pct for year in site.years]
        color = PALETTE[index % len(PALETTE)]
        ax.plot(
            [str(year) for year in YEARS],
            values,
            marker="o",
            linewidth=2,
            color=color,
            label=site.display,
        )
        for year, share in zip(YEARS, (item.health_share_pct for item in site.years)):
            if share is None:
                continue
            ax.annotate(
                f"{share:.1f}",
                (str(year), share),
                textcoords="offset points",
                xytext=(0, 7),
                ha="center",
                fontsize=7,
                color=color,
            )
        plotted = True
    ax.set_ylabel("Health share of fact-checks (%)")
    ax.set_xlabel("Year")
    ax.set_title(title, pad=12)
    peaks = []
    for site in sites:
        peaks.extend(year.health_share_pct for year in site.years if year.health_share_pct is not None)
    ax.set_ylim(0, min(100, positive_limit(peaks, scale=1.35, floor=5)))
    if plotted:
        _legend_below(ax, ncol=min(4, max(len(sites), 1)))
    _grid(ax, "y")
    _style_axes(ax)
    _save(fig, path)


def plot_topics_by_site(frame: pd.DataFrame, path: Path) -> None:
    """Grouped horizontal bars: topic percent of health rows, one series per site.

    ``frame`` has topics as the index and one column per site. Blank cells
    mean the percentage is undefined (no health rows, or no topic column).
    """
    title = "Health topics by site, 2022–2025"
    if frame.empty or len(frame.columns) == 0:
        _empty_chart(path, title, "No health-topic distribution to plot")
        return
    # Largest pooled topic should sit at the top. The frame is already in
    # that order, and barh draws the first row at the bottom, so reverse.
    topics = list(frame.index[::-1])
    # Drop a site whose whole column is blank. Those percentages are undefined,
    # and drawing them at zero would look like a measured empty topic mix.
    site_names = [
        name
        for name in frame.columns
        if pd.to_numeric(frame[name], errors="coerce").notna().any()
    ]
    if not site_names:
        _empty_chart(path, title, "No site has a health-topic distribution")
        return
    n_sites = len(site_names)
    n_topics = len(topics)
    fig, ax = _new_fig(10, max(5.5, 0.55 * n_topics + 1.8))
    positions = list(range(n_topics))
    height = 0.8 / max(n_sites, 1)
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    peak_values: list[float] = []
    for index, site_name in enumerate(site_names):
        offsets = [pos + (index - (n_sites - 1) / 2) * height for pos in positions]
        values = [0.0 if pd.isna(value) else float(value) for value in numeric[site_name].tolist()[::-1]]
        peak_values.extend(values)
        bars = ax.barh(
            offsets,
            values,
            height=height * 0.92,
            color=PALETTE[index % len(PALETTE)],
            label=site_name,
            zorder=2,
        )
        _label_horizontal(ax, bars, "pct")
    ax.set_yticks(positions)
    ax.set_yticklabels(topics)
    ax.set_xlabel("Share of the site's health-related fact-checks (%)")
    ax.set_title(title, pad=12)
    ax.set_xlim(0, positive_limit(peak_values, floor=5))
    _legend_below(ax, ncol=min(4, n_sites))
    _grid(ax, "x")
    _style_axes(ax)
    _save(fig, path)


def topics_by_site_frame(sites: list[SiteStats]) -> pd.DataFrame:
    """Topic percent by site. Rows are topics, columns are display names.

    Row order is the pooled health-article count, descending. A topic that a
    site never used is 0.0 when that site has a real health denominator, and
    blank when the site has no topic percentage to compute.
    """
    pooled: Counter[str] = Counter()
    for site in sites:
        for name, count, _pct in site.topics:
            pooled[name] += count
    topic_names = [name for name, _count in sorted(pooled.items(), key=lambda item: (-item[1], item[0].casefold()))]
    columns: dict[str, list[object]] = {}
    for site in sites:
        lookup = {name: pct for name, _count, pct in site.topics}
        # A non-empty topic list means this site has a real health denominator.
        # Topics it does not use are then true zeros. An empty list means the
        # percentage is undefined, so the CSV cell stays blank.
        measurable = bool(site.topics)
        column: list[object] = []
        for topic in topic_names:
            if not measurable:
                column.append("")
            elif topic in lookup:
                column.append(f"{lookup[topic]:.1f}")
            else:
                column.append("0.0")
        columns[site.display] = column
    frame = pd.DataFrame(columns, index=topic_names)
    frame.index.name = "topic"
    return frame


# ---------------------------------------------------------------------------
# Deeper analyses (pooled, plus the per-site health/non-health table)
# ---------------------------------------------------------------------------


def _rate_detail(
    df: pd.DataFrame,
    mask: pd.Series,
    has_column: str,
    positive: pd.Series,
) -> tuple[float | None, int | None, int | None]:
    """Percent, numerator, and denominator for one yes/no field inside ``mask``."""
    base = mask & df[has_column]
    denom = int(base.sum())
    if denom <= 0:
        return None, None, None
    numer = int((base & positive).sum())
    return share_pct(numer, denom), numer, denom


def _rate(df: pd.DataFrame, mask: pd.Series, has_column: str, positive: pd.Series) -> float | None:
    """Percent of ``mask`` rows that are positive, among rows that have the column."""
    pct, _numer, _denom = _rate_detail(df, mask, has_column, positive)
    return pct


def _group_row(df: pd.DataFrame, mask: pd.Series, site_label: str, group: str) -> dict[str, object]:
    """One health or non-health comparison row.

    Numerator and denominator are kept beside each percentage so a base
    below 50 can be written as "19.3%, 11 of 57". The CSV writes the
    percentage columns only.
    """
    fields = {
        "pct_false": ("has_rating_normalized", df["rating_is_false"]),
        "pct_misleading": ("has_rating_normalized", df["rating_is_misleading"]),
        "pct_ai_generated": ("has_ai_generated_misinfo", df["ai_true"]),
        "pct_expert_consultation": ("has_verification_methods", df["token_expert"]),
        "pct_scientific_literature": ("has_verification_methods", df["token_literature"]),
        "pct_linked_sources": ("has_evidence_disclosed", df["is_linked_sources"]),
    }
    row: dict[str, object] = {
        "site": site_label,
        "group": group,
        "n": int(mask.sum()),
    }
    for key, (has_column, positive) in fields.items():
        pct, numer, denom = _rate_detail(df, mask, has_column, positive)
        row[key] = pct
        row[f"numer_{key}"] = numer
        row[f"denom_{key}"] = denom
    return row


def health_vs_nonhealth_records(
    df: pd.DataFrame,
    sites: list[SiteStats],
    view: str,
) -> list[dict[str, object]]:
    """Per site and pooled: health rows against other fact-checks, one view.

    "Other fact-checks" are ``health_related`` false. Unrecognised health
    flags are in neither row. Percentages use the rows inside the group that
    actually have the column being measured. A site with no rows in this view
    is omitted so an empty Fact Crescendo block is not written as zeros.
    """
    records: list[dict[str, object]] = []
    for site in sites:
        part = df.loc[df["site"] == site.site]
        if part.empty:
            continue
        for group, mask in (
            ("health", part["row_is_health"]),
            ("non-health", part["row_is_nonhealth"]),
        ):
            row = _group_row(part, mask, site.display, group)
            row["view"] = view
            records.append(row)
    for group, mask in (("health", df["row_is_health"]), ("non-health", df["row_is_nonhealth"])):
        row = _group_row(df, mask, "Pooled", group)
        row["view"] = view
        records.append(row)
    return records


def health_vs_nonhealth_frame(records: list[dict[str, object]]) -> pd.DataFrame:
    """CSV form of the health vs non-health comparison, with a view column."""
    columns = ["view", "site", "group", "n", *[key for key, _label in HVNH_METRICS]]
    rows = []
    for record in records:
        row: dict[str, object] = {
            "view": record.get("view", ""),
            "site": record["site"],
            "group": record["group"],
            "n": "" if record["n"] is None else str(int(record["n"])),
        }
        for key, _label in HVNH_METRICS:
            value = record[key]
            row[key] = "" if value is None else f"{float(value):.1f}"
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def plot_health_vs_nonhealth(records: list[dict[str, object]], path: Path, view_name: str = CHART_VIEW_LABEL) -> None:
    """Grouped horizontal bars for the pooled health vs non-health comparison."""
    title = f"Health vs non-health fact-checks, {view_name}, 2022–2025"
    pooled = {row["group"]: row for row in records if row["site"] == "Pooled"}
    health = pooled.get("health")
    other = pooled.get("non-health")
    if health is None or other is None or (int(health["n"]) == 0 and int(other["n"]) == 0):
        _empty_chart(path, title, "No fact-checks with health_related coded")
        return
    labels = []
    health_values = []
    other_values = []
    for key, label in HVNH_METRICS:
        if health[key] is None and other[key] is None:
            continue
        labels.append(label)
        # None is an absent bar. A real 0% is a zero-length bar.
        health_values.append(float("nan") if health[key] is None else float(health[key]))
        other_values.append(float("nan") if other[key] is None else float(other[key]))
    if not labels:
        _empty_chart(path, title, "Comparison metrics are missing from the coded files")
        return
    # First metric at the top.
    labels = list(reversed(labels))
    health_values = list(reversed(health_values))
    other_values = list(reversed(other_values))
    positions = list(range(len(labels)))
    fig, ax = _new_fig(10, max(4.8, 0.7 * len(labels) + 1.6))
    height = 0.36
    bars_h = ax.barh(
        [pos + height / 2 for pos in positions],
        health_values,
        height=height,
        color=MAIN,
        label=f"Health-related (n={int(health['n']):,})",
        zorder=2,
    )
    bars_o = ax.barh(
        [pos - height / 2 for pos in positions],
        other_values,
        height=height,
        color=SECOND,
        label=f"Other fact-checks (n={int(other['n']):,})",
        zorder=2,
    )
    _label_horizontal(ax, bars_h, "pct")
    _label_horizontal(ax, bars_o, "pct")
    ax.set_yticks(positions)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Share of articles in the group (%)")
    ax.set_title(title, pad=12)
    ax.set_xlim(0, positive_limit(health_values + other_values, floor=5))
    _legend_below(ax, ncol=2)
    _grid(ax, "x")
    _style_axes(ax)
    _save(fig, path)


def topics_over_time(df: pd.DataFrame, view: str) -> tuple[pd.DataFrame, dict[int, int], int]:
    """Pooled topic percent of health articles in each year.

    Returns the long table, the health-row denominator by year (rows that
    have ``health_topic``), and how many health rows lacked the topic column.
    A year with a zero denominator is omitted from the table. A topic that
    is absent in a year with a positive denominator is stored as 0.
    """
    health = df["row_is_health"]
    omitted = int((health & ~df["has_health_topic"]).sum())
    coded = df.loc[health & df["has_health_topic"]]
    denoms = {year: int((coded["year"] == year).sum()) for year in YEARS}
    labels = coded["health_topic"].map(label_or_blank)
    observed = sorted(set(labels.tolist()), key=str.casefold)
    # Stable topic order: total count descending, so the stacked chart and
    # the CSV list the largest theme first.
    totals = labels.value_counts()
    observed.sort(key=lambda topic: (-int(totals.get(topic, 0)), topic.casefold()))

    rows: list[dict[str, object]] = []
    for year in YEARS:
        denom = denoms[year]
        if denom <= 0:
            continue
        year_labels = labels.loc[coded["year"] == year]
        counts = year_labels.value_counts()
        for topic in observed:
            count = int(counts.get(topic, 0))
            rows.append(
                {
                    "view": view,
                    "year": str(year),
                    "topic": topic,
                    "count": str(count),
                    "pct": f"{(share_pct(count, denom) or 0.0):.1f}",
                }
            )
    frame = pd.DataFrame(rows, columns=["view", "year", "topic", "count", "pct"])
    return frame, denoms, omitted


def plot_topics_by_year(
    frame: pd.DataFrame,
    denoms: dict[int, int],
    path: Path,
    view_name: str = CHART_VIEW_LABEL,
) -> None:
    """Stacked bars of topic percent by year. Each bar sums to about 100."""
    title = f"Health topic mix by year, {view_name}, 2022–2025"
    if frame.empty:
        _empty_chart(path, title, "No health-related fact-checks with a topic")
        return
    topics: list[str] = []
    for topic in frame["topic"].tolist():
        if topic not in topics:
            topics.append(topic)
    fig, ax = _new_fig(9, 5.6)
    bottoms = [0.0 for _ in YEARS]
    year_labels = [str(year) for year in YEARS]
    for index, topic in enumerate(topics):
        values = []
        for year in YEARS:
            if denoms.get(year, 0) <= 0:
                values.append(0.0)
                continue
            match = frame.loc[(frame["year"] == str(year)) & (frame["topic"] == topic), "pct"]
            values.append(float(match.iloc[0]) if len(match) else 0.0)
        bars = ax.bar(
            year_labels,
            values,
            bottom=bottoms,
            color=PALETTE[index % len(PALETTE)],
            width=0.72,
            label=topic,
            zorder=2,
        )
        for bar, value in zip(bars, values):
            # Small slices stay unlabeled so the stack remains readable.
            # The CSV has the exact one-decimal share for every slice.
            if value < 6:
                continue
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_y() + value / 2,
                f"{value:.0f}",
                ha="center",
                va="center",
                fontsize=7,
                color="white" if value >= 12 else "#1a1a1a",
            )
        bottoms = [bottom + value for bottom, value in zip(bottoms, values)]
    ax.set_ylabel("Share of health-related fact-checks (%)")
    ax.set_xlabel("Year")
    ax.set_ylim(0, 100)
    ax.set_title(title, pad=12)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(
        handles[::-1],
        labels[::-1],
        frameon=False,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        title="Topic",
    )
    _style_axes(ax)
    _save(fig, path)


def platform_by_topic(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, int]], int]:
    """Counts for the top 6 health topics crossed with the top 6 platforms.

    Top 6 is by article count. A tie on the sixth place is broken by name
    so the table is reproducible. An article naming several platforms
    increments each of those platforms once; a repeated token in one cell
    increments that platform once.

    The returned rank list is every platform among health rows, not only
    the top 6, so the report can name the most common platform as well as
    the largest cell. The integer is the number of health rows that have a
    platforms value (the share denominator).
    """
    health = df.loc[df["row_is_health"] & df["has_platforms"] & df["has_health_topic"]]
    if health.empty:
        empty = pd.DataFrame()
        return empty, [], 0

    topic_counts: Counter[str] = Counter()
    platform_counts: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    for topic_raw, platforms_raw in zip(health["health_topic"].tolist(), health["platforms"].tolist()):
        topic = label_or_blank(topic_raw)
        topic_counts[topic] += 1
        tokens = set(split_tokens(platforms_raw))
        for token in tokens:
            platform_counts[token] += 1
            pair_counts[(topic, token)] += 1

    top_topics = [name for name, _count in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:6]]
    top_platforms = [name for name, _count in sorted(platform_counts.items(), key=lambda item: (-item[1], item[0].casefold()))[:6]]
    matrix = [
        [int(pair_counts.get((topic, platform), 0)) for platform in top_platforms]
        for topic in top_topics
    ]
    frame = pd.DataFrame(matrix, index=top_topics, columns=top_platforms)
    frame.index.name = "topic"
    ranked = sorted(platform_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return frame, ranked, int(len(health))


def platform_long_frame(frame: pd.DataFrame, view: str) -> pd.DataFrame:
    """Long form of one view's topic × platform counts, including a view column."""
    columns = ["view", "topic", "platform", "count"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    named = frame.copy()
    named.columns.name = "platform"
    long = named.stack().reset_index()
    long.columns = ["topic", "platform", "count"]
    long.insert(0, "view", view)
    long["count"] = long["count"].map(lambda value: str(int(value)))
    return long[columns]


def plot_platform_heatmap(frame: pd.DataFrame, path: Path, view_name: str = CHART_VIEW_LABEL) -> None:
    """Heatmap of topic × platform counts, with the count written in each cell."""
    title = f"Platforms by health topic, {view_name}, 2022–2025"
    if frame.empty:
        _empty_chart(path, title, "No health-related rows with both a topic and a platform")
        return
    matrix = frame.to_numpy(dtype=float)
    vmax = float(matrix.max()) if matrix.size else 0.0
    cmap = LinearSegmentedColormap.from_list("study_teal", ["#F4FBFA", MAIN])
    fig_w = max(8, 1.15 * frame.shape[1] + 3)
    fig_h = max(4.5, 0.7 * frame.shape[0] + 2)
    fig, ax = _new_fig(fig_w, fig_h)
    image = ax.imshow(matrix, cmap=cmap, aspect="auto", vmin=0, vmax=max(vmax, 1))
    ax.set_xticks(list(range(frame.shape[1])))
    ax.set_xticklabels(list(frame.columns), rotation=30, ha="right")
    ax.set_yticks(list(range(frame.shape[0])))
    ax.set_yticklabels(list(frame.index))
    threshold = vmax * 0.62
    for row_index in range(frame.shape[0]):
        for col_index in range(frame.shape[1]):
            value = int(matrix[row_index, col_index])
            color = "white" if vmax > 0 and value >= threshold else "#1a1a1a"
            ax.text(col_index, row_index, str(value), ha="center", va="center", color=color, fontsize=9)
    ax.set_xlabel("Platform (an article can count in more than one)")
    ax.set_ylabel("Health topic")
    ax.set_title(title, pad=12)
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label("Health-related fact-checks")
    _save(fig, path)


def ai_trend_frame(df: pd.DataFrame, view: str) -> pd.DataFrame:
    """Pooled AI-generated share by year, for health and for other fact-checks.

    The denominator each year is the group size among rows that have
    ``ai_generated_misinfo``. A year with no such rows leaves the percent blank.
    """
    rows = []
    for year in YEARS:
        part = df.loc[df["year"] == year]
        health = part["row_is_health"] & part["has_ai_generated_misinfo"]
        other = part["row_is_nonhealth"] & part["has_ai_generated_misinfo"]
        health_n = int(health.sum())
        other_n = int(other.sum())
        health_ai = int((health & part["ai_true"]).sum()) if health_n else 0
        other_ai = int((other & part["ai_true"]).sum()) if other_n else 0
        health_pct = share_pct(health_ai, health_n)
        other_pct = share_pct(other_ai, other_n)
        rows.append(
            {
                "view": view,
                "year": str(year),
                "health_n": str(health_n),
                "health_ai_n": str(health_ai),
                "health_ai_pct": "" if health_pct is None else f"{health_pct:.1f}",
                "nonhealth_n": str(other_n),
                "nonhealth_ai_n": str(other_ai),
                "nonhealth_ai_pct": "" if other_pct is None else f"{other_pct:.1f}",
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "view",
            "year",
            "health_n",
            "health_ai_n",
            "health_ai_pct",
            "nonhealth_n",
            "nonhealth_ai_n",
            "nonhealth_ai_pct",
        ],
    )


def plot_ai_trend(frame: pd.DataFrame, path: Path, view_name: str = CHART_VIEW_LABEL) -> None:
    """Two lines: AI-generated share of health claims and of other fact-checks."""
    title = f"AI-generated claims by year, {view_name}, 2022–2025"
    if frame.empty or not bool(df_any_ai(frame)):
        _empty_chart(path, title, "ai_generated_misinfo is missing")
        return
    fig, ax = _new_fig(8.5, 5)
    series = (
        ("health_ai_pct", "Health-related", MAIN),
        ("nonhealth_ai_pct", "Other fact-checks", SECOND),
    )
    peaks: list[float] = []
    for column, label, color in series:
        values = []
        for raw in frame[column].tolist():
            if raw == "" or raw is None:
                values.append(float("nan"))
            else:
                values.append(float(raw))
                peaks.append(float(raw))
        ax.plot([str(year) for year in YEARS], values, marker="o", linewidth=2, color=color, label=label)
        for year, raw in zip(YEARS, frame[column].tolist()):
            if raw == "" or raw is None:
                continue
            ax.annotate(
                f"{float(raw):.1f}",
                (str(year), float(raw)),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=8,
                color=color,
            )
    ax.set_ylabel("AI-generated share (%)")
    ax.set_xlabel("Year")
    ax.set_title(title, pad=12)
    ax.set_ylim(0, min(100, positive_limit(peaks, scale=1.4, floor=5)))
    _legend_below(ax, ncol=2)
    _grid(ax, "y")
    _style_axes(ax)
    _save(fig, path)


def df_any_ai(frame: pd.DataFrame) -> bool:
    """True when at least one AI percent in the trend table was computed."""
    for column in ("health_ai_pct", "nonhealth_ai_pct"):
        for value in frame[column].tolist():
            if value not in ("", None):
                return True
    return False


# ---------------------------------------------------------------------------
# Report prose — every figure is taken from the computed stats
# ---------------------------------------------------------------------------


def _year_row(stats: YearStats, label: str, include_adjusted: bool) -> list[object]:
    row = [
        label,
        f"{stats.rows:,}",
        f"{stats.fact_checks:,}",
        fmt_int(stats.health),
        fmt_pct(stats.health_share_pct).removesuffix("%") if stats.health_share_pct is not None else "n/a",
        fmt_int(stats.health_misinfo),
    ]
    if include_adjusted:
        row.extend(
            [
                fmt_int(stats.fact_checks_excl_fc_intl),
                fmt_int(stats.health_excl_fc_intl),
                fmt_pct(stats.health_share_excl_fc_intl_pct).removesuffix("%")
                if stats.health_share_excl_fc_intl_pct is not None
                else "n/a",
                fmt_int(stats.health_misinfo_excl_fc_intl),
            ]
        )
    return row


def _valid_trend_years(site: SiteStats) -> list[YearStats]:
    """Years used for a site's trend: at least 100 fact-checks and a health share."""
    return [
        year
        for year in site.years
        if year.fact_checks >= LOW_N_FACT_CHECKS and year.health_share_pct is not None and year.health is not None
    ]


def _trend_sentence(site: SiteStats, style: ShareStyle | None = None) -> str:
    """Health-share trend, skipping low-n years and using a 1-point threshold.

    Direction is the change in the percentage of fact-checks, from the first
    valid year to the last. A move smaller than one percentage point is
    described as roughly flat. Raw health-article counts are not the trend.
    """
    style = style or ShareStyle()
    exclusion = low_n_exclusion_sentence(site)
    if not site.availability.health:
        return (
            "A trend in the health share is unavailable because health_related is missing."
            + exclusion
        )
    valid = _valid_trend_years(site)
    if len(valid) < 2:
        return (
            "A trend in the health share is not calculated because fewer than two years "
            "have at least 100 fact-checks."
            + exclusion
        )
    start, end = valid[0], valid[-1]
    verb = share_move(start.health_share_pct, end.health_share_pct)
    start_txt = style.pct(start.health_share_pct, start.health, start.fact_checks)
    end_txt = style.pct(end.health_share_pct, end.health, end.fact_checks)
    if verb == "is roughly flat":
        sentence = (
            f"The health share of fact-checks is roughly flat, {start_txt} in {start.year} "
            f"and {end_txt} in {end.year}."
        )
    else:
        sentence = (
            f"The health share of fact-checks {verb} from {start_txt} in {start.year} "
            f"to {end_txt} in {end.year}."
        )
    return sentence + exclusion


def draft_paragraphs(site: SiteStats) -> tuple[str, str]:
    """Two draft paragraphs in the requested academic shape.

    Paragraph 1 is volume, the health share, and health misinformation.
    Fact Crescendo also gets the adjusted view, which drops its rows coded
    International. Paragraph 2 is the leading topic, verdicts, AI,
    international claims, and the 2022-to-2025 trend.
    """
    total = site.total
    display = site.display
    style = ShareStyle()
    if total.health is None:
        first = (
            f"Health-related screening totals are unavailable for {display} because "
            f"the health_related column is missing from its files. The site contributes "
            f"{total.rows:,} articles, of which {total.fact_checks:,} are in the fact-check denominator."
        )
        second = (
            "Topic, verdict, platform, and AI figures are unavailable for the same reason. "
            + _trend_sentence(site, style)
            + small_base_note(style)
        )
        return first, second

    if total.health_share_pct is None:
        share_clause = "an undefined share (the fact-check denominator is zero)"
    else:
        share_clause = "approximately " + style.pct(total.health_share_pct, total.health, total.fact_checks)
    first = (
        f"Initial LLM screening identifies {total.health:,} articles as health-related, "
        f"corresponding to {share_clause} of {display}'s fact-checking output between 2022 and 2025 "
        f"({total.fact_checks:,} fact-checks out of {total.rows:,} articles)."
    )
    if total.health_misinfo is None:
        first += " The health-misinformation count is unavailable because rating_normalized is missing."
    else:
        first += (
            f" Of these, {total.health_misinfo:,} meet the study definition of health misinformation "
            f"(a normalised verdict of False, Misleading, or Unproven)."
        )
        if site.true_n:
            first += (
                f" Articles rated True ({site.true_n:,}) remain in the health count."
            )
    if is_fact_crescendo(site.site):
        if total.health_excl_fc_intl is None:
            first += " The adjusted view was not computed because health_related is absent."
        else:
            if total.health_share_excl_fc_intl_pct is None:
                adjusted_share = "an undefined share"
            else:
                adjusted_share = "approximately " + style.pct(
                    total.health_share_excl_fc_intl_pct,
                    total.health_excl_fc_intl,
                    total.fact_checks_excl_fc_intl,
                )
            adjusted_denom = fmt_int(total.fact_checks_excl_fc_intl)
            misinfo_bit = (
                f" {total.health_misinfo_excl_fc_intl:,} of them are health misinformation."
                if total.health_misinfo_excl_fc_intl is not None
                else ""
            )
            first += (
                " After excluding this site's international product series "
                "(rows coded claim_region International), "
                f"health-related articles number {total.health_excl_fc_intl:,}, or {adjusted_share} "
                f"of the remaining fact-checks ({adjusted_denom}).{misinfo_bit} {REGION_CAVEAT}"
            )

    if total.health is None:
        topic_sentence = "The leading thematic category is unavailable because health_related is missing."
    elif total.health == 0:
        topic_sentence = "No health-related fact-checks were identified, so there is no leading thematic category."
    elif not site.topics and not site.availability.topic:
        topic_sentence = "The leading thematic category is unavailable because health_topic is missing."
    elif not site.topics:
        topic_sentence = (
            "The leading thematic category is unavailable because health-related rows have no health_topic value."
        )
    else:
        top_count = max(count for _name, count, _pct in site.topics)
        leaders = [(name, count, pct) for name, count, pct in site.topics if count == top_count]
        pct = leaders[0][2]
        topic_share = style.pct(pct, top_count, total.health)
        # style.pct already appends "n of d" when the base is below 50, so the
        # parenthetical is only added for larger bases.
        names = [name for name, _count, _pct in leaders]
        if total.health is not None and total.health < SMALL_BASE:
            # "21.9%, 7 of 32" already carries the count, so it sits directly
            # before "health-related fact-checks".
            if len(names) == 1:
                topic_sentence = (
                    f"{names[0]} is the largest thematic category, accounting for "
                    f"{topic_share} health-related fact-checks."
                )
            else:
                topic_sentence = (
                    f"{join_and(names)} are the largest thematic categories, each accounting for "
                    f"{topic_share} health-related fact-checks."
                )
        elif len(names) == 1:
            topic_sentence = (
                f"{names[0]} is the largest thematic category, accounting for approximately "
                f"{topic_share} of health-related fact-checks ({top_count:,} of {total.health:,})."
            )
        else:
            topic_sentence = (
                f"{join_and(names)} are the largest thematic categories, each accounting for approximately "
                f"{topic_share} of health-related fact-checks ({top_count:,} of {total.health:,})."
            )

    if site.ratings:
        rating_base = site.false_or_misleading_denom if site.false_or_misleading_denom is not None else total.health
        small_rating = rating_base is not None and rating_base < SMALL_BASE
        bits = []
        for name, count, pct in site.ratings:
            shown = style.pct(pct, count, rating_base)
            if small_rating:
                bits.append(f"{name} accounts for {shown}")
            else:
                bits.append(f"{name} accounts for {shown} ({count:,})")
        rating_sentence = "Among health-related fact-checks, " + join_and(bits) + "."
    elif total.health is None:
        rating_sentence = "Verdict shares are unavailable because health_related is missing."
    elif total.health == 0:
        rating_sentence = "Verdict shares are undefined because the health denominator is zero."
    elif not site.availability.rating:
        rating_sentence = "Verdict shares are unavailable because rating_normalized is missing."
    else:
        rating_sentence = "Verdict shares are unavailable because health-related rows have no rating_normalized value."

    extra_bits = []
    if site.ai_pct is not None and site.ai_n is not None and site.ai_denom is not None:
        ai_shown = style.pct(site.ai_pct, site.ai_n, site.ai_denom)
        if site.ai_denom < SMALL_BASE:
            extra_bits.append(f"AI-generated claims account for {ai_shown}")
        else:
            extra_bits.append(
                f"Approximately {ai_shown} are described as AI-generated "
                f"({site.ai_n:,} of {site.ai_denom:,} health-related rows with an AI flag)"
            )
    elif total.health and not site.availability.ai:
        extra_bits.append("The AI-generated share is unavailable because ai_generated_misinfo is missing")
    if site.intl_pct is not None and site.intl_n is not None and site.intl_denom is not None:
        intl_shown = style.pct(site.intl_pct, site.intl_n, site.intl_denom)
        if site.intl_denom < SMALL_BASE:
            extra_bits.append(f"claims coded International account for {intl_shown}")
        else:
            extra_bits.append(
                f"claims coded International account for approximately {intl_shown} "
                f"({site.intl_n:,} of {site.intl_denom:,})"
            )
    elif total.health and not site.availability.region:
        extra_bits.append("the international share is unavailable because claim_region is missing")
    if extra_bits:
        # First clause is a sentence start; any further clause stays joined to it.
        detail = extra_bits[0][0].upper() + extra_bits[0][1:]
        if len(extra_bits) > 1:
            detail += ", and " + extra_bits[1]
        detail += "."
    else:
        detail = ""

    second = " ".join(
        part for part in (topic_sentence, rating_sentence, detail, _trend_sentence(site, style)) if part
    )
    second += small_base_note(style)
    return first, second


def key_findings(sites: list[SiteStats], df: pd.DataFrame) -> list[str]:
    """Three to five cross-site findings. Each number is computed here."""
    bullets: list[str] = []
    style = ShareStyle()

    ranked = [site for site in sites if site.total.health_share_pct is not None]
    if ranked:
        best = max(site.total.health_share_pct for site in ranked)
        leaders = [site for site in ranked if site.total.health_share_pct == best]
        if len(leaders) == 1:
            site = leaders[0]
            share_txt = style.pct(site.total.health_share_pct, site.total.health, site.total.fact_checks)
            sentence = (
                f"{site.display} has the largest health share, at {share_txt} "
                f"of fact-checks ({fmt_int(site.total.health)} of {site.total.fact_checks:,})"
            )
            if site.total.health_misinfo is not None:
                sentence += f", of which {site.total.health_misinfo:,} are health misinformation"
            sentence += "."
            if (
                is_fact_crescendo(site.site)
                and site.total.health_share_excl_fc_intl_pct is not None
            ):
                adjusted_txt = style.pct(
                    site.total.health_share_excl_fc_intl_pct,
                    site.total.health_excl_fc_intl,
                    site.total.fact_checks_excl_fc_intl,
                )
                sentence += (
                    f" Its health share after excluding its international product series is {adjusted_txt}. "
                    f"{REGION_CAVEAT}"
                )
        else:
            sentence = (
                f"{join_and([site.display for site in leaders])} share the largest health share, "
                f"at {fmt_pct(best)} of fact-checks."
            )
        bullets.append(sentence)
    else:
        bullets.append("Health shares could not be computed because health_related is missing.")

    # Most common topic overall: count health rows in the pooled frame,
    # which is the article-weighted total rather than an average of site percents.
    health = df.loc[df["row_is_health"] & df["has_health_topic"]]
    if len(health):
        labels = health["health_topic"].map(label_or_blank)
        counts = labels.value_counts()
        top_count = int(counts.max())
        names = sorted(str(name) for name, count in counts.items() if int(count) == top_count)
        pct = share_pct(top_count, int(len(health)))
        topic_txt = style.pct(pct, top_count, int(len(health)))
        if len(names) == 1:
            bullets.append(
                f"Across all sites, {names[0]} is the most common health topic "
                f"({top_count:,} articles, {topic_txt} of {len(health):,} health-related fact-checks with a topic)."
            )
        else:
            bullets.append(
                f"Across all sites, {join_and(names)} tie as the most common health topic "
                f"(each {top_count:,} articles, {topic_txt} of {len(health):,} health-related fact-checks with a topic)."
            )
    else:
        bullets.append("No pooled health topic can be named because no health-related row has a health_topic value.")

    intl_sites = [site for site in sites if site.intl_n is not None]
    if intl_sites:
        best_n = max(site.intl_n or 0 for site in intl_sites)
        leaders = [site for site in intl_sites if site.intl_n == best_n]
        bits = [
            f"{site.display} ({site.intl_n:,} of {site.intl_denom:,} health-related articles with a region, {fmt_pct(site.intl_pct)})"
            for site in leaders
        ]
        if len(leaders) == 1:
            site = leaders[0]
            intl_txt = style.pct(site.intl_pct, site.intl_n, site.intl_denom)
            bullets.append(
                f"{site.display} has the most health-related claims coded International "
                f"({site.intl_n:,} of {site.intl_denom:,} health-related articles with a region, "
                f"{intl_txt})."
            )
        else:
            bullets.append(
                "The most health-related claims coded International are tied between " + join_and(bits) + "."
            )
    else:
        bullets.append("International health claims could not be compared because claim_region is missing.")

    trend_bits = []
    for site in sites:
        valid = _valid_trend_years(site)
        exclusion = low_n_exclusion_sentence(site).strip()
        if not site.availability.health:
            trend_bits.append(f"{site.display} has no health-share trend because health_related is missing")
        elif len(valid) < 2:
            trend_bits.append(
                f"{site.display} has no health-share trend because fewer than two years have at least 100 fact-checks"
            )
        else:
            start, end = valid[0], valid[-1]
            verb = share_move(start.health_share_pct, end.health_share_pct)
            start_txt = style.pct(start.health_share_pct, start.health, start.fact_checks)
            end_txt = style.pct(end.health_share_pct, end.health, end.fact_checks)
            if verb == "is roughly flat":
                trend_bits.append(
                    f"{site.display}'s health share is roughly flat ({start_txt} in {start.year} and {end_txt} in {end.year})"
                )
            else:
                trend_bits.append(
                    f"{site.display}'s health share {verb} from {start_txt} in {start.year} to {end_txt} in {end.year}"
                )
        if exclusion:
            # exclusion already ends with a period; the bullet adds the final one.
            trend_bits[-1] += ". " + exclusion.strip().rstrip(".")
    bullets.append("Health-share trends, low-n years excluded: " + "; ".join(trend_bits) + ".")

    shifts = []
    for site in sites:
        all_share = site.total.health_share_pct
        adjusted_share = site.total.health_share_excl_fc_intl_pct
        if all_share is None or adjusted_share is None:
            continue
        shifts.append((abs(all_share - adjusted_share), all_share - adjusted_share, site))
    if shifts:
        widest = max(item[0] for item in shifts)
        chosen = [item for item in shifts if item[0] == widest]
        # Name every site that shares the widest absolute change.
        pieces = []
        for _abs_delta, signed, site in chosen:
            all_txt = style.pct(site.total.health_share_pct, site.total.health, site.total.fact_checks)
            adjusted_txt = style.pct(
                site.total.health_share_excl_fc_intl_pct,
                site.total.health_excl_fc_intl,
                site.total.fact_checks_excl_fc_intl,
            )
            pieces.append(
                f"{site.display} ({all_txt} of all fact-checks to "
                f"{adjusted_txt} after excluding that series, "
                f"{fmt_pp(signed)})"
            )
        bullets.append(
            "Excluding Fact Crescendo's international product series changes the health share most at "
            + join_and(pieces)
            + ". "
            + REGION_CAVEAT
        )

    return bullets[:5]


def _lookup_group(records: list[dict[str, object]], site: str, group: str) -> dict[str, object] | None:
    for record in records:
        if record["site"] == site and record["group"] == group:
            return record
    return None


def _metric_clause(label: str, health_pct: float | None, other_pct: float | None) -> str | None:
    if health_pct is None and other_pct is None:
        return None
    return f"{label} is {fmt_pct(health_pct)} of health-related fact-checks and {fmt_pct(other_pct)} of other fact-checks"


def _contrast_sign(health_pct: object, other_pct: object) -> str:
    """Whether the health percentage is higher, lower, or within 1 point."""
    if health_pct is None or other_pct is None:
        return "missing"
    delta = float(
        (Decimal(str(health_pct)) - Decimal(str(other_pct))).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )
    if delta >= TREND_PP:
        return "higher"
    if delta <= -TREND_PP:
        return "lower"
    return "similar"


def _paired_metric(style: ShareStyle, label: str, health: dict, other: dict, key: str) -> str | None:
    health_pct = health.get(key)
    other_pct = other.get(key)
    if health_pct is None and other_pct is None:
        return None
    health_txt = style.pct(health_pct, health.get(f"numer_{key}"), health.get(f"denom_{key}"))
    other_txt = style.pct(other_pct, other.get(f"numer_{key}"), other.get(f"denom_{key}"))
    return f"{label} is {health_txt} of health-related fact-checks and {other_txt} of other fact-checks"


def _topic_snapshot(frame: pd.DataFrame, denoms: dict[int, int]) -> dict[str, object]:
    """Leading topics and the largest share changes, using a 1-point threshold."""
    snapshot: dict[str, object] = {
        "lead_2022": None,
        "pct_2022": None,
        "lead_2025": None,
        "pct_2025": None,
        "rise": None,
        "fall": None,
        "defined": False,
    }
    denom_2022 = denoms.get(2022, 0)
    denom_2025 = denoms.get(2025, 0)
    if frame.empty or denom_2022 <= 0 or denom_2025 <= 0:
        return snapshot
    topics = list(dict.fromkeys(frame["topic"].tolist()))

    def pct_of(year: int, topic: str) -> float:
        match = frame.loc[(frame["year"] == str(year)) & (frame["topic"] == topic), "pct"]
        if len(match) == 0 or match.iloc[0] == "":
            return 0.0
        return float(match.iloc[0])

    def leaders(year: int) -> tuple[list[str], float]:
        scores = [(topic, pct_of(year, topic)) for topic in topics]
        best = max(score for _topic, score in scores)
        names = sorted(topic for topic, score in scores if score == best)
        return names, best

    lead_2022, pct_2022 = leaders(2022)
    lead_2025, pct_2025 = leaders(2025)

    def quantized_delta(topic: str) -> float:
        change = Decimal(str(pct_of(2025, topic))) - Decimal(str(pct_of(2022, topic)))
        return float(change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    deltas = [(topic, quantized_delta(topic)) for topic in topics]
    rise = max(delta for _topic, delta in deltas)
    fall = min(delta for _topic, delta in deltas)
    rise_names = sorted(topic for topic, delta in deltas if delta == rise) if rise >= TREND_PP else []
    fall_names = sorted(topic for topic, delta in deltas if delta == fall) if fall <= -TREND_PP else []
    snapshot.update(
        {
            "lead_2022": lead_2022,
            "pct_2022": pct_2022,
            "lead_2025": lead_2025,
            "pct_2025": pct_2025,
            "denom_2022": denom_2022,
            "denom_2025": denom_2025,
            "rise_names": rise_names,
            "rise": rise if rise_names else 0.0,
            "fall_names": fall_names,
            "fall": fall if fall_names else 0.0,
            "pct_of": pct_of,
            "defined": True,
        }
    )
    return snapshot


def _ai_direction(frame: pd.DataFrame, column: str) -> str:
    """Share direction from 2022 to 2025 for one AI series. Undefined if an end is missing."""
    by_year = {int(row.year): row for row in frame.itertuples(index=False)}
    start = getattr(by_year.get(2022), column, None) if 2022 in by_year else None
    end = getattr(by_year.get(2025), column, None) if 2025 in by_year else None
    if start in ("", None) or end in ("", None):
        return "undefined"
    return share_move(float(start), float(end))


def deeper_findings(payloads: dict[str, dict]) -> list[str]:
    """One bullet per pooled analysis, with all three views side by side.

    Each bullet names the site that contributes the most rows and says whether
    the headline result holds across views or is driven by Fact Crescendo or
    by international claims. Trend direction inside these bullets uses the
    health or topic share and a one-percentage-point threshold.
    """
    style = ShareStyle()
    bullets: list[str] = []

    # --- Health versus non-health ---
    hvnh_bits: list[str] = []
    hvnh_signs: dict[str, object] = {}
    for view_id, label in VIEW_SPECS:
        payload = payloads[view_id]
        records = payload["hvnh"]
        health = _lookup_group(records, "Pooled", "health")
        other = _lookup_group(records, "Pooled", "non-health")
        if health is None or other is None:
            hvnh_bits.append(f"{label}: comparison unavailable.")
            hvnh_signs[view_id] = ("missing",)
            continue
        hvnh_signs[view_id] = tuple(
            _contrast_sign(health.get(key), other.get(key)) for key, _name in HVNH_METRICS
        )
        clauses = [
            clause
            for clause in (
                _paired_metric(style, "A False verdict", health, other, "pct_false"),
                _paired_metric(style, "A Misleading verdict", health, other, "pct_misleading"),
                _paired_metric(style, "An AI-generated flag", health, other, "pct_ai_generated"),
                _paired_metric(style, "Expert consultation", health, other, "pct_expert_consultation"),
                _paired_metric(style, "Scientific literature", health, other, "pct_scientific_literature"),
                _paired_metric(style, "Linked sources", health, other, "pct_linked_sources"),
            )
            if clause
        ]
        frame = payload["df"]
        contribution = dominant_rows(frame, frame["row_is_health"], "health-related fact-checks")
        if clauses:
            hvnh_bits.append(
                f"{label} (health-related n={int(health['n']):,}; other fact-checks n={int(other['n']):,}): "
                + ". ".join(clauses)
                + f". {contribution[0].upper() + contribution[1:]}."
            )
        else:
            hvnh_bits.append(f"{label}: the comparison columns are missing. {contribution[0].upper() + contribution[1:]}.")
    bullets.append(
        "Health versus non-health. "
        + " ".join(hvnh_bits)
        + " "
        + stability_sentence(hvnh_signs, "health versus non-health contrast")
    )

    # --- Topic mix ---
    topic_bits: list[str] = []
    topic_signs: dict[str, object] = {}
    for view_id, label in VIEW_SPECS:
        payload = payloads[view_id]
        snap = _topic_snapshot(payload["topic_frame"], payload["topic_denoms"])
        frame = payload["df"]
        health_mask = frame["row_is_health"] & frame["has_health_topic"]
        contribution = dominant_rows(frame, health_mask, "health-related fact-checks with a topic")
        year_bits = []
        for year in (2022, 2025):
            year_mask = health_mask & (frame["year"] == year)
            year_bits.append(
                f"in {year}, {dominant_rows(frame, year_mask, 'health-related fact-checks with a topic')}"
            )
        if not snap["defined"]:
            topic_bits.append(
                f"{label}: the 2022-to-2025 topic comparison is undefined because one of those years "
                f"has no health-related fact-checks with a topic. {contribution[0].upper() + contribution[1:]}. "
                + "; ".join(year_bits)
                + "."
            )
            topic_signs[view_id] = ("undefined",)
            continue
        lead_2022 = join_and(snap["lead_2022"])
        lead_2025 = join_and(snap["lead_2025"])
        topic_signs[view_id] = (
            lead_2022,
            lead_2025,
            join_and(snap["rise_names"]) if snap["rise_names"] else "flat",
            join_and(snap["fall_names"]) if snap["fall_names"] else "flat",
        )
        sentence = (
            f"{label}: in 2022 the leading health topic is {lead_2022} "
            f"({fmt_pct(snap['pct_2022'])} of {snap['denom_2022']:,}); "
            f"in 2025 it is {lead_2025} ({fmt_pct(snap['pct_2025'])} of {snap['denom_2025']:,})."
        )
        pct_of = snap["pct_of"]
        if snap["rise_names"]:
            parts = [
                f"{name} from {fmt_pct(pct_of(2022, name))} to {fmt_pct(pct_of(2025, name))}"
                for name in snap["rise_names"]
            ]
            sentence += " The largest rise is " + join_and(parts) + f" ({fmt_pp(snap['rise'])})."
        if snap["fall_names"]:
            parts = [
                f"{name} from {fmt_pct(pct_of(2022, name))} to {fmt_pct(pct_of(2025, name))}"
                for name in snap["fall_names"]
            ]
            sentence += " The largest fall is " + join_and(parts) + f" ({fmt_pp(snap['fall'])})."
        if not snap["rise_names"] and not snap["fall_names"]:
            sentence += " Topic shares are roughly flat from 2022 to 2025."
        sentence += f" {contribution[0].upper() + contribution[1:]}; " + "; ".join(year_bits) + "."
        topic_bits.append(sentence)
    bullets.append(
        "Topic mix over time. "
        + " ".join(topic_bits)
        + " "
        + stability_sentence(topic_signs, "topic-mix finding")
    )

    # --- Platform by topic ---
    platform_bits: list[str] = []
    platform_signs: dict[str, object] = {}
    for view_id, label in VIEW_SPECS:
        payload = payloads[view_id]
        ranked = payload["platform_ranked"]
        denom = payload["platform_denom"]
        wide = payload["platform_frame"]
        frame = payload["df"]
        base = frame["row_is_health"] & frame["has_platforms"] & frame["has_health_topic"]
        contribution = dominant_rows(frame, base, "health-related fact-checks with a topic and a platform")
        if wide.empty or denom <= 0 or not ranked:
            platform_bits.append(f"{label}: the platform-by-topic table is empty. {contribution[0].upper() + contribution[1:]}.")
            platform_signs[view_id] = ("empty",)
            continue
        top_name, top_count = ranked[0]
        top_pct = share_pct(top_count, denom)
        stacked = wide.stack()
        max_count = int(stacked.max()) if len(stacked) else 0
        ties = sorted(
            (str(topic), str(platform))
            for (topic, platform), count in stacked.items()
            if int(count) == max_count
        )
        if len(ties) == 1:
            pair_text = f"{ties[0][0]} × {ties[0][1]} ({max_count:,})"
            pair_sign = f"{ties[0][0]} × {ties[0][1]}"
        elif len(ties) <= 3:
            pair_text = join_and([f"{topic} × {platform}" for topic, platform in ties]) + f" (each {max_count:,})"
            pair_sign = join_and([f"{topic} × {platform}" for topic, platform in ties])
        else:
            pair_text = f"{len(ties)} topic–platform pairs tie at {max_count:,}"
            pair_sign = f"{len(ties)}-way tie"
        platform_signs[view_id] = (top_name, pair_sign)
        platform_bits.append(
            f"{label}: the most named platform is {top_name} "
            f"({style.pct(top_pct, top_count, denom)} of {denom:,} articles). "
            f"The largest cell is {pair_text}. {contribution[0].upper() + contribution[1:]}."
        )
    bullets.append(
        "Platforms by topic. "
        + " ".join(platform_bits)
        + " "
        + stability_sentence(platform_signs, "platform finding")
    )

    # --- AI trend ---
    ai_bits: list[str] = []
    ai_signs: dict[str, object] = {}
    for view_id, label in VIEW_SPECS:
        payload = payloads[view_id]
        ai_frame = payload["ai_frame"]
        frame = payload["df"]
        health_mask = frame["row_is_health"] & frame["has_ai_generated_misinfo"]
        contribution = dominant_rows(frame, health_mask, "health-related fact-checks with an AI flag")
        year_2025 = dominant_rows(
            frame,
            health_mask & (frame["year"] == 2025),
            "health-related fact-checks with an AI flag in 2025",
        )
        if ai_frame.empty or not df_any_ai(ai_frame):
            ai_bits.append(f"{label}: the AI-generated trend was skipped because ai_generated_misinfo is missing.")
            ai_signs[view_id] = ("undefined", "undefined")
            continue
        by_year = {int(row.year): row for row in ai_frame.itertuples(index=False)}

        def point(year: int, pct_col: str, numer_col: str, denom_col: str) -> tuple[float | None, int, int]:
            row = by_year[year]
            pct_raw = getattr(row, pct_col)
            pct = None if pct_raw in ("", None) else float(pct_raw)
            return pct, int(getattr(row, numer_col)), int(getattr(row, denom_col))

        def one_series(pct_col: str, numer_col: str, denom_col: str, series_label: str) -> str:
            start_pct, start_n, start_d = point(2022, pct_col, numer_col, denom_col)
            end_pct, end_n, end_d = point(2025, pct_col, numer_col, denom_col)
            if start_pct is None or end_pct is None:
                return f"The {series_label} AI-generated share is undefined at an endpoint."
            verb = share_move(start_pct, end_pct)
            start_txt = style.pct(start_pct, start_n, start_d)
            end_txt = style.pct(end_pct, end_n, end_d)
            if verb == "is roughly flat":
                return (
                    f"The {series_label} AI-generated share is roughly flat, "
                    f"{start_txt} in 2022 and {end_txt} in 2025."
                )
            return (
                f"The {series_label} AI-generated share {verb} from {start_txt} in 2022 "
                f"to {end_txt} in 2025."
            )

        health_dir = _ai_direction(ai_frame, "health_ai_pct")
        other_dir = _ai_direction(ai_frame, "nonhealth_ai_pct")
        ai_signs[view_id] = (health_dir, other_dir)
        ai_bits.append(
            f"{label}: {one_series('health_ai_pct', 'health_ai_n', 'health_n', 'health-related')} "
            f"{one_series('nonhealth_ai_pct', 'nonhealth_ai_n', 'nonhealth_n', 'other-fact-check')} "
            f"{contribution[0].upper() + contribution[1:]}; {year_2025}."
        )
    bullets.append(
        "AI-generated trend. "
        + " ".join(ai_bits)
        + " "
        + stability_sentence(ai_signs, "AI-generated trend")
    )
    return bullets


def render_summary_markdown(records: list[dict[str, object]]) -> str:
    """Markdown twin of summary_table.csv, with readable headers."""
    headers = [
        "Site",
        "Fact-checks",
        "Health",
        "Health share %",
        "Health misinfo",
        "Health (excl. FC intl series)",
        "Health share % (excl. FC intl series)",
        "Top topic",
        "Top topic %",
        "False or Misleading %",
        "AI-generated %",
        "International %",
    ]
    rows = []
    for record in records:
        rows.append(
            [
                record["site"],
                fmt_int(record["fact_checks"]),
                fmt_int(record["health"]),
                fmt_pct(record["health_share_pct"]).removesuffix("%") if record["health_share_pct"] is not None else "n/a",
                fmt_int(record["health_misinfo"]),
                fmt_int(record["health_excl_fc_intl"]),
                fmt_pct(record["health_share_excl_fc_intl_pct"]).removesuffix("%")
                if record["health_share_excl_fc_intl_pct"] is not None
                else "n/a",
                record["top_topic"] if record["top_topic"] else "n/a",
                f"{record['top_topic_pct']:.1f}" if record["top_topic_pct"] is not None else "n/a",
                f"{record['pct_false_or_misleading']:.1f}" if record["pct_false_or_misleading"] is not None else "n/a",
                f"{record['pct_ai_generated']:.1f}" if record["pct_ai_generated"] is not None else "n/a",
                f"{record['pct_international']:.1f}" if record["pct_international"] is not None else "n/a",
            ]
        )
    return markdown_table(headers, rows)


def render_site_section(site: SiteStats) -> str:
    """Key numbers, chart links, and the two draft paragraphs for one outlet."""
    show_adjusted = is_fact_crescendo(site.site)
    headers = [
        "Year",
        "Articles",
        "Fact-checks",
        "Health",
        "Health share %",
        "Health misinfo",
    ]
    if show_adjusted:
        headers.extend(
            [
                "Fact-checks (excl. intl series)",
                "Health (excl. intl series)",
                "Health share % (excl. intl series)",
                "Health misinfo (excl. intl series)",
            ]
        )
    year_rows = [_year_row(year, year_table_label(year), show_adjusted) for year in site.years]
    year_rows.append(_year_row(site.total, "Total", show_adjusted))
    lines = [
        f"## {site.display}",
        "",
        f"Source site id: `{site.site}`.",
        "",
        markdown_table(headers, year_rows),
        "",
    ]
    if show_adjusted:
        lines.extend(
            [
                "Health share is health-related fact-checks divided by fact-checks in that column's view. "
                "The adjusted columns drop this site's rows coded claim_region International. "
                "BOOM, India Today, and PTI are not given those columns, because the adjusted view keeps every one of their rows.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Health share is health-related fact-checks divided by fact-checks.",
                "",
            ]
        )

    if site.topics:
        lines.extend(
            [
                "Health topics (count and percent of health-related fact-checks):",
                "",
                markdown_table(
                    ["Topic", "Count", "% of health"],
                    [[name, f"{count:,}", f"{pct:.1f}"] for name, count, pct in site.topics],
                ),
                "",
            ]
        )
    if site.ratings:
        lines.extend(
            [
                "Normalised verdicts among health-related fact-checks:",
                "",
                markdown_table(
                    ["Verdict", "Count", "% of health"],
                    [[name, f"{count:,}", f"{pct:.1f}"] for name, count, pct in site.ratings],
                ),
                "",
            ]
        )
    if site.ai_pct is not None:
        lines.append(
            f"AI-generated: {fmt_int(site.ai_n)} of {fmt_int(site.ai_denom)} health-related rows "
            f"({fmt_pct(site.ai_pct)})."
        )
    if site.intl_pct is not None:
        lines.append(
            f"International claims: {fmt_int(site.intl_n)} of {fmt_int(site.intl_denom)} health-related rows "
            f"({fmt_pct(site.intl_pct)})."
        )
    if site.platforms:
        top = ", ".join(f"{name} ({count:,})" for name, count, _pct in site.platforms[:8])
        lines.append(f"Platforms named on health-related fact-checks (top 8): {top}.")
    if site.methods:
        top = ", ".join(f"{name} ({count:,})" for name, count, _pct in site.methods[:8])
        lines.append(f"Verification methods on health-related fact-checks: {top}.")
    if site.evidence:
        top = ", ".join(f"{name} ({count:,})" for name, count in site.evidence)
        lines.append(f"Evidence disclosed on health-related fact-checks: {top}.")
    if site.caveats:
        lines.extend(["", "Notes on this site:"])
        lines.extend(f"- {caveat}" for caveat in site.caveats)

    folder = site.dirname
    lines.extend(
        [
            "",
            f"![{site.display} fact-checks by year]({folder}/articles_by_year.png)",
            "",
            f"![{site.display} health-related fact-checks by year]({folder}/health_by_year.png)",
            "",
            "Bars are health-related fact-checks. The line is their share of all fact-checks that year.",
            "",
            f"![{site.display} health topics]({folder}/topics.png)",
            "",
            f"![{site.display} verdicts among health-related fact-checks]({folder}/ratings.png)",
            "",
            f"![{site.display} platforms among health-related fact-checks]({folder}/platforms.png)",
            "",
            "### Draft write-up",
            "",
        ]
    )
    first, second = draft_paragraphs(site)
    lines.extend([first, "", second, ""])
    return "\n".join(lines)


def render_data_notes(
    notes: list[FileNote],
    df_window: pd.DataFrame,
    df_all: pd.DataFrame,
    corrections: list[AppliedCorrection],
    corrections_found: bool,
) -> str:
    """Source-file audit: rows, date disagreements, missing columns, LLM reminder."""
    headers = [
        "File",
        "Year",
        "Rows",
        "Fact-checks in 2022–2025",
        "Date year differs",
        "Date unparsed",
        "Missing columns",
    ]
    rows = []
    for note in notes:
        if note.skipped:
            rows.append([note.file, "n/a", "n/a", "n/a", "n/a", "n/a", note.skipped])
            continue
        missing = ", ".join(note.missing_columns) if note.missing_columns else "—"
        rows.append(
            [
                note.file,
                "" if note.year is None else str(note.year),
                f"{note.rows:,}",
                fmt_int(note.fact_checks),
                "n/a" if note.date_mismatch is None else f"{note.date_mismatch:,}",
                "n/a" if note.date_unparsed is None else f"{note.date_unparsed:,}",
                missing,
            ]
        )
    lines = [
        "## Data notes",
        "",
        "Year is the four-digit token in `_full_<year>_`. Article dates are parsed with "
        "`format=\"mixed\"`, `dayfirst=True`, and `errors=\"coerce\"`, and are used only "
        "to count disagreements with the filename year. A date whose calendar year differs "
        "from the filename is counted under \"Date year differs\". A date that could not be "
        "parsed is counted under \"Date unparsed\" and is not also counted as a disagreement.",
        "",
        markdown_table(headers, rows),
        "",
    ]

    outside = df_all.loc[~df_all["year"].isin(YEARS)]
    if len(outside):
        counts = outside.groupby(["source_file", "year"]).size()
        bits = [f"{file} ({int(year)}: {int(count):,} rows)" for (file, year), count in counts.items()]
        lines.append(
            "Rows outside 2022–2025 are excluded from every total and chart: " + "; ".join(bits) + "."
        )
        lines.append("")

    for note in notes:
        if note.site_fill:
            lines.append(f"`{note.file}`: {note.site_fill}.")
        if note.bool_unrecognised:
            detail = ", ".join(f"{column} {count:,}" for column, count in sorted(note.bool_unrecognised.items()))
            lines.append(
                f"`{note.file}`: unrecognised Yes/No/True/False values (left missing, not treated as False): {detail}."
            )
        if note.status_not_ok:
            lines.append(f"`{note.file}`: {note.status_not_ok:,} rows have a status other than ok. They are retained.")
        if note.status_blank:
            lines.append(f"`{note.file}`: {note.status_blank:,} rows have a blank status. They are retained.")
    flagged = [(note.file, note.duplicate_flags) for note in notes if note.duplicate_flags]
    if any(note.duplicate_flags is not None for note in notes):
        total_flags = sum(count for _file, count in flagged)
        n_files = sum(1 for note in notes if note.duplicate_flags is not None)
        lines.append(
            f"`possible_duplicate_title` is present on {n_files} file(s) and flags {total_flags:,} rows. "
            "Flagged rows are kept in every total."
        )

    # Region values outside the protocol set, among rows that have the column.
    if "has_claim_region" in df_window.columns and bool(df_window["has_claim_region"].any()):
        regions = df_window.loc[df_window["has_claim_region"], "claim_region"].map(cell_str)
        unexpected = regions.loc[~regions.isin(KNOWN_REGIONS | {""})]
        if len(unexpected):
            counts = unexpected.value_counts()
            detail = ", ".join(f"{label} ({int(count):,})" for label, count in counts.items())
            lines.append(f"claim_region values outside India / International / Unclear: {detail}.")
        blank = int((regions == "").sum())
        if blank:
            lines.append(
                f"{blank:,} rows with a claim_region column have a blank region. "
                "They stay in the adjusted view because a blank is not the code International."
            )

    versions: Counter[str] = Counter()
    for note in notes:
        versions.update(note.prompt_versions)
    if versions:
        detail = ", ".join(f"{label} {count:,}" for label, count in sorted(versions.items()))
        lines.append(f"Observed prompt_version values: {detail}.")

    low_n_bits = []
    if "row_is_fact" in df_window.columns:
        for site_id, site_frame in df_window.groupby("site", sort=False):
            for year in YEARS:
                year_frame = site_frame.loc[site_frame["year"] == year]
                if year_frame.empty:
                    continue
                n_fact = int(year_frame["row_is_fact"].sum())
                if n_fact < LOW_N_FACT_CHECKS:
                    low_n_bits.append(f"{display_name(str(site_id))} {year} ({n_fact:,} fact-checks)")
    if low_n_bits:
        lines.append(
            "Low n (fewer than 100 fact-checks; excluded from trend calculations): "
            + "; ".join(low_n_bits)
            + "."
        )

    lines.extend(
        [
            "",
            "Labels for `is_fact_check`, `health_related`, `health_topic`, `rating_normalized`, "
            "`platforms`, `verification_methods`, `evidence_disclosed`, `ai_generated_misinfo`, "
            "and `claim_region` are LLM-generated (prompt v3) and are pending manual validation. "
            "Figures in this report are preliminary.",
            "",
            "### Manual corrections",
            "",
        ]
    )
    if not corrections_found:
        lines.append("No `corrections.csv` was found, so no manual corrections were applied.")
        lines.append("")
        lines.append("0 LLM labels corrected after manual review.")
    else:
        for item in corrections:
            topic_bit = (
                ' `health_topic` was also set to "Not health-related".'
                if item.topic_reset
                else ""
            )
            lines.append(
                f"- `{item.site}`: `{item.field}` set to {item.new_value}.{topic_bit} "
                f'Matched title: "{item.matched_title}". Reason: {item.reason}.'
            )
        if not corrections:
            lines.append("No correction matched exactly one record, so none were applied.")
        lines.append("")
        lines.append(f"{len(corrections)} LLM labels corrected after manual review.")
    lines.append("")
    return "\n".join(lines)


def write_report(
    path: Path,
    sites: list[SiteStats],
    records: list[dict[str, object]],
    findings: list[str],
    deeper: list[str],
    notes: list[FileNote],
    df_window: pd.DataFrame,
    df_all: pd.DataFrame,
    topic_omitted: int,
    corrections: list[AppliedCorrection],
    corrections_found: bool,
) -> None:
    """Assemble analysis_report.md from the same objects that were written to CSV."""
    site_list = join_and([site.display for site in sites])
    opening = [
        "# Health misinformation in Indian fact-checking, 2022–2025",
        "",
        f"This report summarises LLM-assisted coding of fact-checks from {site_list}. "
        "The coding protocol is prompt v3. Year is taken from each filename (`_full_<year>_`), "
        "which is authoritative because the scrapes were split by year. "
        "The fact-check denominator is rows with `is_fact_check` true; when that column is missing "
        "from a file, every row in that file is kept. Health-related rows are fact-checks with "
        "`health_related` true. Health misinformation is the health subset whose normalised verdict "
        "is False, Misleading, or Unproven. Headline health figures are shown for all claims and "
        "again after excluding Fact Crescendo rows coded `claim_region` International "
        "(that outlet's international product series). Every other site keeps all of its rows in that view. "
        "A site-year with fewer than 100 fact-checks is marked low n and is left out of trend statements. "
        "Trend direction uses the share of fact-checks and is described only when that share changes by "
        "at least 1 percentage point; a smaller change is roughly flat. "
        "Counts below are computed from the classified CSVs and are preliminary until manual validation.",
        "",
        "## Cross-site summary",
        "",
        "Machine-readable copy: `summary_table.csv`. Topic percentages by site: `topics_by_site.csv`.",
        "",
        render_summary_markdown(records),
        "",
        "The columns headed excl. FC intl series drop Fact Crescendo rows coded `claim_region` International. "
        "BOOM, India Today, and PTI keep every row in that view, so their two health counts match.",
        "",
        "![Health share by site](health_share_by_site.png)",
        "",
        "![Health share by year](health_share_by_year.png)",
        "",
        "![Health topics by site](topics_by_site.png)",
        "",
        "### Key findings",
        "",
    ]
    opening.extend(f"- {bullet}" for bullet in findings)
    opening.append("")

    site_sections = [render_site_section(site) for site in sites]

    deeper_lines = [
        "## Deeper findings",
        "",
        "Tables: `health_vs_nonhealth.csv`, `topics_by_year.csv`, `platform_by_topic.csv`, `ai_trend.csv`. "
        "Each table has a `view` column for all claims, the pool excluding Fact Crescendo's international product series, and the pool with Fact Crescendo removed.",
        "",
        "Health and non-health percentages use fact-checks as the base. A fact-check with "
        "`health_related` true is health-related; one with `health_related` false is another fact-check. "
        "Unrecognised `health_related` values are in neither group. "
        "Topic shares are the percentage of that year's health-related fact-checks; a topic with no "
        "articles in a year that does have health-related fact-checks is 0% for the change calculation. "
        "A topic-share change smaller than 1 percentage point is described as roughly flat. "
        "Platform counts allow an article to contribute to more than one platform. "
        "AI shares are the percentage of each group flagged `ai_generated_misinfo`. "
        "The charts use the view that excludes Fact Crescendo's international product series. "
        "The bullets state the same finding in all three views.",
        "",
    ]
    if topic_omitted:
        deeper_lines.append(
            f"{topic_omitted:,} health-related rows have no health_topic and are omitted from the topic-mix table."
        )
        deeper_lines.append("")
    deeper_lines.extend(f"- {bullet}" for bullet in deeper)
    deeper_lines.extend(
        [
            "",
            f"![Health vs non-health fact-checks, {CHART_VIEW_LABEL}](health_vs_nonhealth.png)",
            "",
            f"![Health topic mix by year, {CHART_VIEW_LABEL}](topics_by_year.png)",
            "",
            f"![Platforms by health topic, {CHART_VIEW_LABEL}](platform_by_topic.png)",
            "",
            f"![AI-generated claims by year, {CHART_VIEW_LABEL}](ai_trend.png)",
            "",
        ]
    )

    parts = [
        "\n".join(opening),
        "\n".join(site_sections),
        "\n".join(deeper_lines),
        render_data_notes(notes, df_window, df_all, corrections, corrections_found),
    ]
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def print_summary(outdir: Path, sites: list[SiteStats], df: pd.DataFrame) -> None:
    """Short console recap. The full numbers live in the report and the CSVs."""
    n_rows = int(len(df))
    n_fact = int(df["row_is_fact"].sum())
    n_health = int(df["row_is_health"].sum())
    print(f"Rows in 2022–2025: {n_rows:,}")
    print(f"Fact-checks: {n_fact:,}")
    print(f"Health-related: {n_health:,} ({fmt_pct(share_pct(n_health, n_fact))})")
    for site in sites:
        total = site.total
        print(
            f"{site.display}: fact-checks={total.fact_checks:,}, "
            f"health={fmt_int(total.health)} ({fmt_pct(total.health_share_pct)}), "
            f"health misinfo={fmt_int(total.health_misinfo)}"
        )
    print(f"Report: {outdir / 'analysis_report.md'}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyse classified fact-check CSVs for the India health-misinformation study.",
    )
    parser.add_argument(
        "--inputs",
        default="data/classified/*_full_*_classified.csv",
        help=(
            "Glob for classified CSVs, resolved from the current working directory. "
            "Default: data/classified/*_full_*_classified.csv"
        ),
    )
    parser.add_argument(
        "--outdir",
        default="analysis",
        help="Directory for tables, charts, and the report. Default: analysis",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    _apply_rcparams()

    # Full file contents, including any filename year outside 2022–2025.
    # Those rows stay available for the data notes and are then dropped
    # from the study window.
    loaded, notes = load_inputs(args.inputs)
    # Manual fixes land before masks and indicators, so every later metric
    # sees the corrected health_related and health_topic values.
    corrections, corrections_found = apply_corrections(loaded, Path("corrections.csv"))
    if corrections_found:
        print(f"Manual corrections applied: {len(corrections)}")
    add_masks(loaded)
    add_indicators(loaded)
    attach_fact_counts(notes, loaded)
    window = loaded.loc[loaded["year"].isin(YEARS)].copy()

    site_ids = order_sites(window["site"].drop_duplicates().tolist())
    if not site_ids:
        raise SystemExit("No classified rows fall in 2022–2025.")
    sites = [compute_site(window.loc[window["site"] == site].copy(), site) for site in site_ids]

    for site in sites:
        plot_site_charts(site, outdir / site.dirname)

    records = summary_records(sites)
    write_table(summary_frame(records), outdir / "summary_table.csv")

    topic_site = topics_by_site_frame(sites)
    # Index is the topic. Writing it as a column keeps the CSV usable.
    topic_site_out = topic_site.reset_index()
    write_table(topic_site_out, outdir / "topics_by_site.csv")

    plot_health_share_by_site(sites, outdir / "health_share_by_site.png")
    plot_health_share_by_year(sites, outdir / "health_share_by_year.png")
    plot_topics_by_site(topic_site, outdir / "topics_by_site.png")

    # Three sensitivity views of every pooled deeper analysis. Charts use the
    # view that drops Fact Crescendo's international product series. The
    # all-claims omitted-topic count is a property of the files, so it is
    # counted once on the full window.
    _topic_all, _denoms_all, topic_omitted = topics_over_time(window, "all_claims")
    payloads: dict[str, dict] = {}
    hvnh_frames = []
    topic_frames = []
    platform_frames = []
    ai_frames = []
    for view_id, _label in VIEW_SPECS:
        part = view_frame(window, view_id)
        hvnh_records = health_vs_nonhealth_records(part, sites, view_id)
        topic_year, topic_denoms, _omitted = topics_over_time(part, view_id)
        platform_wide, platform_ranked, platform_denom = platform_by_topic(part)
        ai_frame = ai_trend_frame(part, view_id)
        payloads[view_id] = {
            "df": part,
            "hvnh": hvnh_records,
            "topic_frame": topic_year,
            "topic_denoms": topic_denoms,
            "platform_frame": platform_wide,
            "platform_ranked": platform_ranked,
            "platform_denom": platform_denom,
            "ai_frame": ai_frame,
        }
        hvnh_frames.append(health_vs_nonhealth_frame(hvnh_records))
        topic_frames.append(topic_year)
        platform_frames.append(platform_long_frame(platform_wide, view_id))
        ai_frames.append(ai_frame)

    write_table(pd.concat(hvnh_frames, ignore_index=True), outdir / "health_vs_nonhealth.csv")
    write_table(pd.concat(topic_frames, ignore_index=True), outdir / "topics_by_year.csv")
    write_table(pd.concat(platform_frames, ignore_index=True), outdir / "platform_by_topic.csv")
    write_table(pd.concat(ai_frames, ignore_index=True), outdir / "ai_trend.csv")

    chart = payloads[CHART_VIEW]
    plot_health_vs_nonhealth(chart["hvnh"], outdir / "health_vs_nonhealth.png", CHART_VIEW_LABEL)
    plot_topics_by_year(chart["topic_frame"], chart["topic_denoms"], outdir / "topics_by_year.png", CHART_VIEW_LABEL)
    plot_platform_heatmap(chart["platform_frame"], outdir / "platform_by_topic.png", CHART_VIEW_LABEL)
    plot_ai_trend(chart["ai_frame"], outdir / "ai_trend.png", CHART_VIEW_LABEL)

    findings = key_findings(sites, window)
    deeper = deeper_findings(payloads)
    write_report(
        outdir / "analysis_report.md",
        sites,
        records,
        findings,
        deeper,
        notes,
        window,
        loaded,
        topic_omitted,
        corrections,
        corrections_found,
    )
    print_summary(outdir, sites, window)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Allow piping the console summary to head without a traceback.
        sys.exit(0)
