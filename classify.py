#!/usr/bin/env python3
"""Classify scraped fact-check articles with the Gemini API.

Each article is coded against a system instruction file (default
prompt_v3.txt, beside this script). Articles are sent in batches. After every
finished batch the coded rows are appended to ``<output>.checkpoint.jsonl``,
so the process can be stopped and started again without re-coding finished ids.

Example:
    GEMINI_API_KEY=... python classify.py \\
        --input "data/raw/pti/pti_factcheck_2024.csv" \\
        --site PTI \\
        --output data/classified/pti_2024.csv
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
from google import genai
from google.genai import errors, types
from tqdm import tqdm


# Directory that holds this script. A relative --prompt-file is looked up
# here first, so the default prompt_v3.txt is found from any cwd.
SCRIPT_DIR = Path(__file__).resolve().parent

# How many times a batch may be sent before it is split in half.
# "Retry up to 3 times" is implemented as three attempts total.
MAX_ATTEMPTS = 3

# Waits used after HTTP 429 and 5xx. The first request is immediate; each
# later request follows the next delay in this sequence.
BACKOFF_SECONDS = (5, 10, 20, 40, 80)

# Fields the model must return for every article. "id" is checked separately
# because it also has to match the ids we sent.
REQUIRED_FIELDS = (
    "is_fact_check",
    "primary_claim",
    "health_related",
    "health_topic",
    "rating_original",
    "rating_normalized",
    "platforms",
    "verification_methods",
    "evidence_disclosed",
    "ai_generated_misinfo",
    "claim_region",
    "confidence",
)

# These three are written to the CSV as Yes/No, so they must be JSON booleans.
BOOL_FIELDS = ("is_fact_check", "health_related", "ai_generated_misinfo")

# These two are written as a single cell joined with "; ".
LIST_FIELDS = ("platforms", "verification_methods")

# Free-text fields. health_topic and rating_normalized are strings too, but
# they are checked against the allowed sets below.
STRING_FIELDS = (
    "primary_claim",
    "rating_original",
    "evidence_disclosed",
    "confidence",
)

# Allowed health_topic values. Kept identical to STEP 4 of prompt_v2.txt.
HEALTH_TOPICS = {
    "Infectious diseases",
    "Vaccines",
    "Medicines and treatments",
    "Nutrition and food safety",
    "Maternal and reproductive health",
    "Mental health",
    "Non-communicable diseases",
    "Healthcare services",
    "Alternative medicine",
    "Public health",
    "Symptoms and diagnosis",
    "Other",
    "Not health-related",
}

# Allowed rating_normalized values. Kept identical to STEP 5 of the prompt.
RATINGS = {"False", "Misleading", "Unproven", "True", "Satire", "Not stated"}

# Allowed claim_region values. Kept identical to STEP 8 of the prompt.
CLAIM_REGIONS = {"India", "International", "Unclear"}

# Final CSV column order. "rating" is the model's rating_original.
# article_key sits beside url: it is the url when one exists, otherwise
# "<site>|<date>|<title>". possible_duplicate_title is appended after status
# only when the source CSV has that column.
CSV_COLUMNS = [
    "date",
    "title",
    "author",
    "url",
    "article_key",
    "is_fact_check",
    "primary_claim",
    "health_related",
    "health_topic",
    "rating",
    "rating_normalized",
    "platforms",
    "verification_methods",
    "evidence_disclosed",
    "ai_generated_misinfo",
    "claim_region",
    "confidence",
    "site",
    "model",
    "prompt_version",
    "status",
]

# PTI scrapes sometimes store the checked claim separately from the article
# body. The label order is the order requested for the model prompt.
PTI_LABELS = (("CLAIM", "claim"), ("FACT", "fact"), ("CONCLUSION", "conclusion"))


class ValidationError(Exception):
    """The model response cannot be used as a coding of this batch.

    Raised for malformed JSON, missing or repeated ids, missing fields, and
    values outside the health_topic, rating_normalized, or claim_region
    lists. The caller retries the batch and, after MAX_ATTEMPTS, splits it.
    """


class RunState:
    """Handles shared by every batch in one run.

    sleep_before_next is True after a response has been received, so the
    following request waits ``--sleep`` seconds. Rate-limit waits are applied
    inside the failing call and do not set this flag until a response arrives.
    """

    def __init__(
        self,
        client: genai.Client,
        model: str,
        system_instruction: str,
        prompt_version: str,
        site: str,
        sleep_seconds: float,
        checkpoint_path: Path,
        progress: tqdm,
    ) -> None:
        self.client = client
        self.model = model
        self.system_instruction = system_instruction
        self.prompt_version = prompt_version
        self.site = site
        self.sleep_seconds = sleep_seconds
        self.checkpoint_path = checkpoint_path
        self.progress = progress
        self.sleep_before_next = False


def log(message: str) -> None:
    """Print a message to stderr without overwriting the tqdm bar."""
    tqdm.write(message, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    """Read CLI arguments and reject values that would break the loop."""
    parser = argparse.ArgumentParser(
        description=(
            "Classify scraped fact-check articles with the Gemini API."
        )
    )
    parser.add_argument("--input", required=True, help="Raw article CSV.")
    parser.add_argument(
        "--site",
        required=True,
        help="Site name written to the site column (for example, PTI).",
    )
    parser.add_argument("--output", required=True, help="Classified CSV path.")
    parser.add_argument(
        "--model",
        default="gemini-3.5-flash-lite",
        help="Gemini model id (default: gemini-3.5-flash-lite).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=15,
        help="Articles per request (default: 15).",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=600,
        help=(
            "Maximum words kept from each article body (default: 600). "
            "Longer bodies keep the first 2/3 and the last 1/3."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        default="prompt_v3.txt",
        help="System instruction file (default: prompt_v3.txt).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the input summary and the text for the first 2 articles, "
            "then exit without calling the API."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Classify only the first N articles after filtering. For test runs.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=4,
        help="Seconds to wait between successful API calls (default: 4).",
    )
    args = parser.parse_args()

    # A zero batch size would make the slice loop send nothing forever.
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.max_words < 1:
        parser.error("--max-words must be at least 1")
    if args.sleep < 0:
        parser.error("--sleep must be >= 0")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    return args


def resolve_prompt_path(raw: str) -> Path:
    """Find the system-instruction file.

    An absolute path is used as given. A relative path is taken from the
    script directory when that file exists, and otherwise from the current
    working directory.
    """
    path = Path(raw)
    if path.is_absolute():
        return path
    beside_script = SCRIPT_DIR / path
    if beside_script.is_file():
        return beside_script
    return path


def prompt_version_from_path(path: Path) -> str:
    """Turn a prompt file name into the version written on each row.

    prompt_v3.txt becomes "v3": the directory is ignored, the extension is
    dropped, and a leading "prompt_" is removed. Any other file name keeps
    its stem, so the column still records which file was used.
    """
    stem = path.stem
    prefix = "prompt_"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    return stem


def load_system_instruction(path: Path) -> str:
    """Load the system instruction from the resolved prompt file."""
    if not path.is_file():
        sys.exit(f"System instruction not found: {path}")
    # Keep the prompt text as written. Only drop a single trailing newline so
    # the instruction does not end with an empty extra line.
    return path.read_text(encoding="utf-8").strip("\n")


def make_client() -> genai.Client:
    """Build a Gemini client from the GEMINI_API_KEY environment variable."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set.")
    return genai.Client(api_key=api_key)


def request_config(system_instruction: str) -> types.GenerateContentConfig:
    """Config for one generate_content call.

    automatic_function_calling is disabled because this request has no tools.
    Left on, the SDK logs a warning that direct AFC use in generate_content
    is not recommended, even when the prompt never asks for a function call.
    temperature is 0 so repeated runs of the same batch stay stable.
    response_mime_type asks the model for a raw JSON body.
    """
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0,
        response_mime_type="application/json",
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )


def first_column(columns: list[str], names: tuple[str, ...]) -> str | None:
    """Return the first candidate column name that the CSV actually has.

    Scrapers do not share a schema: title may be "title" or "headline", and
    the body may be "content" or "article_text". The tuple order is the
    preference order.
    """
    for name in names:
        if name in columns:
            return name
    return None


def is_pti_site(site: str) -> bool:
    """True when --site names Press Trust of India.

    "pti" has to be its own token. That accepts "PTI" and "PTI Fact Check"
    and rejects a name that only happens to contain those letters.
    """
    tokens = re.split(r"[^a-z0-9]+", site.lower())
    return "pti" in tokens


def truncate_words(text: str, max_words: int) -> str:
    """Keep the start and the end of a long article body.

    Verdicts usually sit at the end, so a body longer than max_words is not
    cut down to a prefix. The first two thirds of the word budget and the
    last third are kept, with a ``\\n[...]\\n`` marker between them. At the
    default of 600 words that is 400 words, then the marker, then 200.
    Texts that already fit are returned unchanged, original spacing included.
    The two slices cannot overlap: they are built only when the body is
    longer than the budget, and the two lengths add up to that budget.
    """
    words = text.split()
    if len(words) <= max_words:
        return text
    # Integer 2/3. For 600 this is 400; the remainder (200) is the tail.
    head_n = (max_words * 2) // 3
    tail_n = max_words - head_n
    # A budget of 1 gives head_n 0. Guard the tail slice: words[-0:] is the
    # whole list, not an empty one.
    head = " ".join(words[:head_n]) if head_n else ""
    tail = " ".join(words[-tail_n:]) if tail_n else ""
    if head and tail:
        return f"{head}\n[...]\n{tail}"
    return head or tail


def build_model_text(
    row: pd.Series,
    body_column: str | None,
    use_pti_fields: bool,
    max_words: int,
    site_verdict: str | None,
) -> str:
    """Build the "text" field sent to the model for one row.

    The article body is fit to max_words first (head and tail when it is
    longer). For a PTI file that has
    claim, fact, or conclusion columns, those cells are placed in front as
    "CLAIM: ... FACT: ... CONCLUSION: ..." and are not counted against the
    word cap, so a long article cannot push the verdict block off the prompt.
    A newline separates that block from the body.

    site_verdict is the Fact Crescendo "result" cell when that column exists,
    and None when the column is absent. A present column is prepended as
    "SITE VERDICT: <result>\\n" even if the cell is blank, so the model can
    copy rating_original from the organisation's own label.
    """
    body = ""
    if body_column is not None:
        body = truncate_words(str(row[body_column]).strip(), max_words)

    text = body
    if use_pti_fields:
        # Include a label only for columns that exist on this file. An empty
        # cell is still labeled, so the model can see that the scraper had no
        # fact or no conclusion rather than that the column was never collected.
        labeled: list[str] = []
        for label, column in PTI_LABELS:
            if column in row.index:
                labeled.append(f"{label}: {str(row[column]).strip()}")
        if labeled:
            prefix = " ".join(labeled)
            text = f"{prefix}\n{body}" if body else prefix

    if site_verdict is not None:
        # Outermost prefix: the site's verdict comes before PTI labels and body.
        text = f"SITE VERDICT: {site_verdict}\n{text}"
    return text


def blank_series(frame: pd.DataFrame, column: str | None) -> pd.Series:
    """Stripped text from one column, or blanks if that column is not in the file."""
    if column is None or column not in frame.columns:
        return pd.Series("", index=frame.index, dtype="object")
    return frame[column].astype(str).str.strip()


def load_articles(
    path: Path,
    site: str,
    max_words: int,
    limit: int | None,
) -> tuple[list[dict], dict]:
    """Read the raw CSV and return articles plus counts for the input summary.

    Reading with dtype=str and keep_default_na=False keeps empty cells as ""
    and stops pandas from turning the literal strings "NA" or "None" into
    missing values. utf-8-sig drops a BOM if the scraper wrote one.

    A url column is optional. India Today and PTI files have none; Fact
    Crescendo files do. Rows are never dropped for a missing or blank url.
    A row is skipped only when both its title and its content are empty.

    Dedup keeps the first row. A non-empty url is the key. A row with no url
    is keyed by lowercase, stripped title and date. The original row index is
    kept either way, and that index (as a string) is the checkpoint id.

    The returned summary describes the file before --limit is applied.
    """
    if not path.is_file():
        sys.exit(f"Input CSV not found: {path}")

    frame = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    rows_read = len(frame)
    has_url = "url" in frame.columns

    title_column = first_column(list(frame.columns), ("title", "headline"))
    body_column = first_column(list(frame.columns), ("content", "article_text"))
    date_column = "date" if "date" in frame.columns else None
    author_column = "author" if "author" in frame.columns else None
    # Fact Crescendo stores its own verdict here. None means the column is absent.
    result_column = "result" if "result" in frame.columns else None
    duplicate_column = (
        "possible_duplicate_title"
        if "possible_duplicate_title" in frame.columns
        else None
    )

    # PTI's structured fields are used only for that site, and only when at
    # least one of the three columns is present.
    pti_columns_present = any(column in frame.columns for _, column in PTI_LABELS)
    use_pti_fields = is_pti_site(site) and pti_columns_present

    titles = blank_series(frame, title_column)
    contents = blank_series(frame, body_column)
    # Both sides empty: nothing for the model to code. A title with no body,
    # or a body with no title, is still sent.
    empty = (titles == "") & (contents == "")
    rows_skipped_empty = int(empty.sum())
    # Boolean indexing keeps the original index on the surviving rows.
    frame = frame.loc[~empty].copy()

    urls = blank_series(frame, "url" if has_url else None)
    dates = blank_series(frame, date_column)
    titles = blank_series(frame, title_column)
    # Prefixes stop a url from colliding with a title/date string. The text
    # key lowercases both fields; the url key is only stripped, because URL
    # paths can be case-sensitive.
    text_key = "t:" + titles.str.lower() + "\n" + dates.str.lower()
    url_key = "u:" + urls
    frame["_dedup_key"] = url_key.where(urls != "", text_key)
    frame = frame.drop_duplicates(subset=["_dedup_key"], keep="first")
    rows_after_dedup = len(frame)

    if limit is not None:
        frame = frame.head(limit)

    articles: list[dict] = []
    for index, row in frame.iterrows():
        title = str(row[title_column]).strip() if title_column else ""
        date = str(row[date_column]).strip() if date_column else ""
        url = str(row["url"]).strip() if has_url else ""
        # Stable across runs: the url itself, or site|date|title with the
        # same whitespace-stripped date and title written to the CSV. Not
        # lowercased, and not the row index, so resume does not change it.
        article_key = url if url else f"{site}|{date}|{title}"
        site_verdict = str(row[result_column]).strip() if result_column else None
        article = {
            "id": str(index),
            "date": date,
            "title": title,
            "author": str(row[author_column]).strip() if author_column else "",
            "url": url,
            "article_key": article_key,
            "text": build_model_text(
                row,
                body_column,
                use_pti_fields,
                max_words,
                site_verdict,
            ),
        }
        # Carried through for traceability. Not used as a drop rule.
        if duplicate_column is not None:
            article["possible_duplicate_title"] = str(row[duplicate_column]).strip()
        articles.append(article)

    summary = {
        "rows_read": rows_read,
        "rows_after_dedup": rows_after_dedup,
        "rows_skipped_empty": rows_skipped_empty,
        "url_column": has_url,
        "include_possible_duplicate": duplicate_column is not None,
    }
    return articles, summary


def print_input_summary(summary: dict) -> None:
    """Print file counts before any model call.

    rows_after_dedup is the file after empty rows are removed and duplicates
    are collapsed, before --limit. The URL line reports whether the column
    exists, not whether every cell is filled.
    """
    url_found = "found" if summary["url_column"] else "not found"
    print(f"Rows read: {summary['rows_read']}")
    print(f"Rows after dedup: {summary['rows_after_dedup']}")
    print(f"Rows skipped as empty: {summary['rows_skipped_empty']}")
    print(f"URL column: {url_found}")


def checkpoint_path_for(output: Path) -> Path:
    """Return <output>.checkpoint.jsonl for the chosen CSV path.

    The suffix is appended to the whole filename, not substituted for .csv,
    because the request asks for ``<output>.checkpoint.jsonl``.
    """
    return Path(str(output) + ".checkpoint.jsonl")


def load_checkpoint(path: Path) -> dict[str, dict]:
    """Load finished rows keyed by article id.

    A later line for the same id replaces an earlier one. A truncated final
    line (from a kill during the write) is skipped so that article is coded
    again instead of aborting the resume.
    """
    done: dict[str, dict] = {}
    if not path.is_file():
        return done

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                log(f"Skipping unreadable checkpoint line {line_number}.")
                continue
            article_id = str(row.get("id", "")).strip()
            if not article_id:
                log(f"Skipping checkpoint line {line_number} with no id.")
                continue
            done[article_id] = row
    return done


def append_checkpoint(path: Path, rows: list[dict]) -> None:
    """Append one JSON object per finished article and flush it to disk.

    One object per line means a kill mid-write corrupts at most the last
    article, which load_checkpoint then ignores. fsync makes the line durable
    before the next batch starts, so a crash after this returns does not
    lose the batch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def is_retryable_api_error(exc: BaseException) -> bool:
    """True for HTTP 429 and any HTTP 5xx raised by the GenAI SDK.

    The SDK stores the HTTP status on APIError.code. 429 is a client error
    (rate limit); 5xx is ServerError. Both are transient and use the backoff
    schedule. Other status codes are not retried here.
    """
    if not isinstance(exc, errors.APIError):
        return False
    code = exc.code
    if not isinstance(code, int):
        return False
    return code == 429 or 500 <= code <= 599


def call_model(state: RunState, batch: list[dict]) -> str:
    """Send one batch and return the raw response text.

    The user message is a JSON array of {id, title, url, text}. Before the
    call, ``--sleep`` is applied if the previous call received a response.
    A 429 or 5xx waits 5s, then 10s, 20s, 40s, and 80s, and is tried again
    after each wait. If those are exhausted, the error is raised and counts
    as one failed attempt in the batch loop.
    """
    if state.sleep_before_next and state.sleep_seconds > 0:
        time.sleep(state.sleep_seconds)
    # Consume the pause even if this call then fails, so a backoff sequence
    # is not preceded by a second --sleep on every retry inside this function.
    state.sleep_before_next = False

    payload = [
        {
            "id": article["id"],
            "title": article["title"],
            "url": article["url"],
            "text": article["text"],
        }
        for article in batch
    ]
    # ensure_ascii=False keeps non-Latin article text as characters, not
    # \\u escapes, which is shorter and easier for the model to read.
    user_message = json.dumps(payload, ensure_ascii=False)
    config = request_config(state.system_instruction)

    # index 0 is the first try (no wait). Each later index follows one backoff.
    waits = (0, *BACKOFF_SECONDS)
    last_error: errors.APIError | None = None
    for index, wait in enumerate(waits):
        if wait:
            log(
                f"Retryable API error ({last_error}); "
                f"waiting {wait}s ({index}/{len(BACKOFF_SECONDS)})."
            )
            time.sleep(wait)
        try:
            response = state.client.models.generate_content(
                model=state.model,
                contents=user_message,
                config=config,
            )
        except errors.APIError as exc:
            last_error = exc
            # More backoff slots remain only when this status is 429 or 5xx.
            if is_retryable_api_error(exc) and index < len(waits) - 1:
                continue
            raise

        # A body came back, so the next request (retry or next batch) waits
        # the normal --sleep. An empty body is still a completed call.
        state.sleep_before_next = True
        text = response.text
        if text is None or not text.strip():
            raise ValidationError("model returned an empty response")
        return text

    # The loop either returns or raises. This guards the type checker.
    if last_error is not None:
        raise last_error
    raise ValidationError("model call produced no response")


def parse_json_array(raw: str) -> list:
    """Parse the model body into a list.

    response_mime_type is application/json, so the body should be a bare
    array. A markdown fence is stripped anyway, because some model builds
    still wrap JSON in ```json ... ```.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop the opening ``` or ```json line.
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"response is not valid JSON ({exc})") from exc

    if not isinstance(parsed, list):
        raise ValidationError(
            f"response JSON is a {type(parsed).__name__}, expected an array"
        )
    return parsed


def validate_response(raw: str, batch: list[dict]) -> dict[str, dict]:
    """Parse and check one model response.

    Every input id must appear exactly once. Every required field must be
    present and have a usable type. health_topic and rating_normalized must
    be one of the codebook values. The returned dict is keyed by id.
    """
    parsed = parse_json_array(raw)
    expected_ids = [article["id"] for article in batch]

    if len(parsed) != len(expected_ids):
        raise ValidationError(
            f"expected {len(expected_ids)} objects, got {len(parsed)}"
        )

    by_id: dict[str, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            raise ValidationError("response array contains a non-object")
        if "id" not in item:
            raise ValidationError("a response object is missing id")

        # The model sometimes returns the id as a JSON number. Compare strings
        # so 12 and "12" are the same article.
        item_id = str(item["id"]).strip()
        if item_id in by_id:
            raise ValidationError(f"duplicate id {item_id}")
        by_id[item_id] = item

    expected_set = set(expected_ids)
    missing = [item_id for item_id in expected_ids if item_id not in by_id]
    extra = [item_id for item_id in by_id if item_id not in expected_set]
    if missing or extra:
        raise ValidationError(f"id mismatch; missing={missing} extra={extra}")

    for item_id in expected_ids:
        check_item(by_id[item_id], item_id)

    return by_id


def check_item(item: dict, item_id: str) -> None:
    """Validate one coded article and normalize its enum fields in place."""
    for field in REQUIRED_FIELDS:
        if field not in item or item[field] is None:
            raise ValidationError(f"id {item_id} is missing {field}")

    # Yes/No output needs a real JSON boolean. The string "false" is rejected
    # so the batch is retried rather than silently coded as Yes.
    for field in BOOL_FIELDS:
        if not isinstance(item[field], bool):
            raise ValidationError(f"id {item_id} field {field} is not a boolean")

    for field in LIST_FIELDS:
        value = item[field]
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            raise ValidationError(
                f"id {item_id} field {field} is not a list of strings"
            )

    for field in (*STRING_FIELDS, "health_topic", "rating_normalized", "claim_region"):
        if not isinstance(item[field], str):
            raise ValidationError(f"id {item_id} field {field} is not a string")

    # Strip only for the membership test, then store the canonical spelling
    # so the CSV does not keep accidental surrounding whitespace.
    topic = item["health_topic"].strip()
    if topic not in HEALTH_TOPICS:
        raise ValidationError(f"id {item_id} health_topic {topic!r} is not allowed")
    item["health_topic"] = topic

    rating = item["rating_normalized"].strip()
    if rating not in RATINGS:
        raise ValidationError(
            f"id {item_id} rating_normalized {rating!r} is not allowed"
        )
    item["rating_normalized"] = rating

    region = item["claim_region"].strip()
    if region not in CLAIM_REGIONS:
        raise ValidationError(f"id {item_id} claim_region {region!r} is not allowed")
    item["claim_region"] = region


def format_bool(value: bool) -> str:
    """Write a model boolean as Yes or No."""
    return "Yes" if value else "No"


def format_list(values: list[str]) -> str:
    """Join a list field with '; ' for a single CSV cell.

    Blank entries are dropped so a trailing empty string does not leave a
    dangling separator.
    """
    parts = [entry.strip() for entry in values if entry.strip()]
    return "; ".join(parts)


def source_fields(article: dict, state: RunState) -> dict:
    """Columns that come from the scrape and the run, not from the model.

    possible_duplicate_title is copied only when the source file had that
    column. article_key is always copied. The checkpoint id stays the row
    index; article_key is not used to resume.
    """
    row = {
        "id": article["id"],
        "date": article["date"],
        "title": article["title"],
        "author": article["author"],
        "url": article["url"],
        "article_key": article["article_key"],
        "site": state.site,
        "model": state.model,
        "prompt_version": state.prompt_version,
    }
    if "possible_duplicate_title" in article:
        row["possible_duplicate_title"] = article["possible_duplicate_title"]
    return row


def to_output_row(article: dict, coded: dict, state: RunState) -> dict:
    """Turn one validated model object into a checkpoint/CSV row.

    The checkpoint keeps "id" so a resume can skip the article. The CSV
    writer drops "id" because it is not one of the requested columns.
    "rating" is rating_original, the organisation's own verdict wording.
    """
    row = source_fields(article, state)
    row.update(
        {
            "is_fact_check": format_bool(coded["is_fact_check"]),
            "primary_claim": coded["primary_claim"],
            "health_related": format_bool(coded["health_related"]),
            "health_topic": coded["health_topic"],
            "rating": coded["rating_original"],
            "rating_normalized": coded["rating_normalized"],
            "platforms": format_list(coded["platforms"]),
            "verification_methods": format_list(coded["verification_methods"]),
            "evidence_disclosed": coded["evidence_disclosed"],
            "ai_generated_misinfo": format_bool(coded["ai_generated_misinfo"]),
            "claim_region": coded["claim_region"],
            "confidence": coded["confidence"],
            "status": "ok",
        }
    )
    return row


def failed_row(article: dict, state: RunState) -> dict:
    """Row written when a single article still fails after splitting.

    Source columns are filled so the article can be found again. Model
    columns are blank and status is "failed". The run continues.
    """
    row = source_fields(article, state)
    row.update(
        {
            "is_fact_check": "",
            "primary_claim": "",
            "health_related": "",
            "health_topic": "",
            "rating": "",
            "rating_normalized": "",
            "platforms": "",
            "verification_methods": "",
            "evidence_disclosed": "",
            "ai_generated_misinfo": "",
            "claim_region": "",
            "confidence": "",
            "status": "failed",
        }
    )
    return row


def commit_rows(
    state: RunState,
    rows: list[dict],
    done: dict[str, dict],
) -> None:
    """Persist finished rows, then count them on the progress bar.

    The file is written before the in-memory map is updated. If the write
    fails, those ids stay pending and will be retried. If the process dies
    after the write, the next start loads them from the checkpoint.
    """
    append_checkpoint(state.checkpoint_path, rows)
    for row in rows:
        done[row["id"]] = row
    state.progress.update(len(rows))


def classify_batch(
    state: RunState,
    batch: list[dict],
    done: dict[str, dict],
) -> None:
    """Classify one batch, splitting it when three attempts all fail.

    A successful batch is checkpointed as a whole. If all three attempts fail,
    the batch is split in half and each half goes through this same function,
    including its own three attempts. A single article that still fails is
    checkpointed with status "failed" and does not raise.

    Halves checkpoint themselves. A kill while the second half is running
    therefore does not repeat the first half on resume.
    """
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = call_model(state, batch)
            coded = validate_response(raw, batch)
        except (ValidationError, errors.APIError, OSError) as exc:
            # OSError covers a dropped connection. It uses one of the three
            # attempts but not the 429/5xx backoff, which lives in call_model.
            last_error = exc
            log(
                f"Batch of {len(batch)} failed "
                f"(attempt {attempt}/{MAX_ATTEMPTS}): {exc}"
            )
            continue

        rows = [
            to_output_row(article, coded[article["id"]], state) for article in batch
        ]
        commit_rows(state, rows, done)
        return

    if len(batch) == 1:
        article = batch[0]
        log(f"Giving up on id {article['id']} ({article['url']}): {last_error}")
        commit_rows(state, [failed_row(article, state)], done)
        return

    # Integer division gives the first half the smaller side when the count
    # is odd (15 -> 7 and 8). Both sides are non-empty because len >= 2.
    midpoint = len(batch) // 2
    log(
        f"Splitting batch of {len(batch)} into "
        f"{midpoint} and {len(batch) - midpoint}."
    )
    classify_batch(state, batch[:midpoint], done)
    classify_batch(state, batch[midpoint:], done)


def output_columns(include_possible_duplicate: bool) -> list[str]:
    """CSV columns for this file.

    possible_duplicate_title is last, and only when the raw CSV had it.
    Rows are not dropped because of that column.
    """
    columns = list(CSV_COLUMNS)
    if include_possible_duplicate:
        columns.append("possible_duplicate_title")
    return columns


def write_output(
    path: Path,
    articles: list[dict],
    done: dict[str, dict],
    columns: list[str],
) -> pd.DataFrame:
    """Write the classified CSV in input order and return the frame.

    Order follows the filtered article list, not checkpoint order, so a
    resumed run still lines up with the source file. utf-8-sig writes a BOM
    so Excel opens the non-ASCII titles correctly.
    """
    records = []
    for article in articles:
        row = done.get(article["id"])
        if row is None:
            # classify_batch commits every id, so this only happens if the
            # checkpoint was edited by hand and an id disappeared.
            sys.exit(f"No result for id {article['id']}; checkpoint is incomplete.")
        records.append({column: row.get(column, "") for column in columns})

    frame = pd.DataFrame(records, columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return frame


def print_summary(frame: pd.DataFrame) -> None:
    """Print total rows, failures, health-related rows, and topic counts.

    Health-related counts cells written as Yes. Topic counts include blank
    cells from failed rows so the numbers can be reconciled with the total.
    """
    total = len(frame)
    failed = int((frame["status"] == "failed").sum())
    health = int((frame["health_related"] == "Yes").sum())

    print(f"Total rows: {total}")
    print(f"Failed: {failed}")
    print(f"Health-related: {health}")
    print("Topic counts:")
    # value_counts sorts by frequency. Blank topic cells (failed rows) are
    # labeled so they are not printed as an empty bullet.
    for topic, count in frame["health_topic"].value_counts(dropna=False).items():
        name = topic if isinstance(topic, str) and topic else "(blank)"
        print(f"  {name}: {int(count)}")


def print_dry_run(articles: list[dict]) -> None:
    """Print the model text of the first two articles.

    This is the "text" field that would go in the user message, after
    truncation and any SITE VERDICT or PTI prefix. Nothing is sent.
    """
    preview = articles[:2]
    if not preview:
        print("No articles to show.")
        return
    for number, article in enumerate(preview, start=1):
        print(f"\n--- article {number} id={article['id']} ---")
        print(article["text"])


def main() -> None:
    """Load articles, code the ones not already checkpointed, write the CSV."""
    args = parse_args()
    output = Path(args.output)
    checkpoint = checkpoint_path_for(output)
    prompt_path = resolve_prompt_path(args.prompt_file)
    prompt_version = prompt_version_from_path(prompt_path)

    articles, summary = load_articles(
        Path(args.input),
        args.site,
        args.max_words,
        args.limit,
    )
    # Counts for the whole file, printed before the checkpoint line and
    # before any request, including a resume that has nothing left to send.
    print_input_summary(summary)
    if args.dry_run:
        print_dry_run(articles)
        return

    done = load_checkpoint(checkpoint)
    pending = [article for article in articles if article["id"] not in done]

    already = len(articles) - len(pending)
    if already:
        log(f"Checkpoint has {already} of these articles; {len(pending)} remaining.")
    else:
        log(f"Classifying {len(pending)} articles.")

    # initial=already fills the bar for rows resumed from the checkpoint,
    # so the bar measures the whole file rather than only this session.
    progress = tqdm(total=len(articles), initial=already, unit="article", desc="Classifying")
    state = RunState(
        client=make_client(),
        model=args.model,
        system_instruction=load_system_instruction(prompt_path),
        prompt_version=prompt_version,
        site=args.site,
        sleep_seconds=args.sleep,
        checkpoint_path=checkpoint,
        progress=progress,
    )

    try:
        for start in range(0, len(pending), args.batch_size):
            batch = pending[start : start + args.batch_size]
            classify_batch(state, batch, done)
    finally:
        # Close the bar even if a request raises something unexpected, so
        # the traceback is not printed on top of a half-drawn bar.
        progress.close()

    frame = write_output(
        output,
        articles,
        done,
        output_columns(summary["include_possible_duplicate"]),
    )
    print(f"Wrote {output}")
    print_summary(frame)


if __name__ == "__main__":
    main()
