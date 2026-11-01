"""LinkedIn metadata -> cached descriptions -> structured LLM analysis -> Sheets."""

import hashlib
import json
import logging
import math
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import gspread
from gspread.exceptions import APIError
from gspread.http_client import HTTPClient
import requests
import yaml
from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.constant import empty_page, headers, jobs_per_page, max_results
from jobspy.model import ScraperInput
from jobspy.util import RequestsRotating, check_status, create_session
from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from config_engine import KeywordFilter, LLMOutput, TitleFilter, column_name

logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
DetectorFactory.seed = 0

CONFIG_DIR = Path(__file__).resolve().parent
OUTPUT = LLMOutput.from_yaml(CONFIG_DIR / "llm_output.yaml")
META_BEFORE = ["name", "company", "location"]
META_AFTER = [
    "job_url", "date_posted", "matches_filters", "analysis_status",
    "description", "analysis_error", "first_seen_at", "search_query", "analysis_model",
    "record_id",
]
if set(OUTPUT.fields) & set(META_BEFORE + META_AFTER):
    raise ValueError("LLM output field names cannot overwrite metadata/cache columns.")
COLUMNS = META_BEFORE + OUTPUT.fields + META_AFTER
LAST_COLUMN = column_name(len(COLUMNS))
ENUMS = OUTPUT.enums
SCHEMA = OUTPUT.schema
PROMPT = OUTPUT.instructions


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def log_event(message, record=None):
    identity = f" record_id={record['record_id']}" if record and record.get("record_id") else ""
    print(f"{utc_timestamp()} {message}{identity}", flush=True)


def linkedin_job_id(url):
    """Accept canonical and slugged LinkedIn job URLs, ignoring tracking parameters."""
    match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", urlsplit(url).path)
    if not match:
        raise ValueError("Cannot extract a LinkedIn job ID from the saved job URL.")
    return match.group(1)


def ensure_record_identity(record):
    """Use first discovery time once; retries and rediscovery keep the same ID."""
    changed = False
    if not record.get("first_seen_at"):
        record["first_seen_at"] = utc_timestamp()
        changed = True
    if not record.get("record_id"):
        try:
            first_seen = datetime.fromisoformat(str(record["first_seen_at"]))
        except ValueError:
            raise ValueError(f"Invalid first_seen_at in row {record.get('_row', '?')}.") from None
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        stamp = first_seen.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        record["record_id"] = f"{linkedin_job_id(record['job_url'])}_{stamp}"
        changed = True
    return changed


def load_catchup_settings(jobs_sheet_name):
    defaults = {
        "enabled": True,
        "state_sheet_name": "Collector_State",
        "overlap_hours": 1,
        "initial_lookback_hours": 24,
    }
    path = CONFIG_DIR / "catchup.yaml"
    if path.exists():
        with path.open(encoding="utf-8") as file:
            configured = yaml.safe_load(file)
        if not isinstance(configured, dict) or set(configured) - set(defaults):
            raise ValueError("Invalid catchup.yaml settings.")
        defaults.update(configured)
    if type(defaults["enabled"]) is not bool:
        raise ValueError("catchup.enabled must be true or false.")
    name = defaults["state_sheet_name"]
    if not isinstance(name, str) or not name.strip() or name == jobs_sheet_name:
        raise ValueError("catchup.state_sheet_name must name a separate worksheet.")
    overlap = defaults["overlap_hours"]
    initial = defaults["initial_lookback_hours"]
    if isinstance(overlap, bool) or not isinstance(overlap, (int, float)) or not math.isfinite(overlap) or overlap < 0:
        raise ValueError("catchup.overlap_hours must be a nonnegative number.")
    if type(initial) is not int or initial < 1:
        raise ValueError("catchup.initial_lookback_hours must be a positive integer.")
    return defaults


def parse_utc_timestamp(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class SearchProgress:
    settings: ScraperInput
    started_at: datetime
    window_start: datetime
    key: str = ""
    pages: int = 0
    ids_discovered: int = 0
    ids_saved: int = 0
    complete: bool = False
    stop_reason: str = "not_finished"
    finalized: bool = False


class CheckpointStore:
    COLUMNS = [
        "search_key", "location", "search_query", "last_completed_search_at",
        "pending_window_start", "last_attempt_started_at", "last_attempt_finished_at",
        "last_attempt_status", "last_attempt_hours_old", "last_attempt_ids",
        "last_attempt_pages", "last_attempt_stop_reason",
    ]

    def __init__(self, spreadsheet, jobs_sheet_name, config):
        self.config = config
        self.jobs_sheet_name = jobs_sheet_name
        self.open_searches = []
        try:
            self.worksheet = spreadsheet.worksheet(config["state_sheet_name"])
        except gspread.WorksheetNotFound:
            self.worksheet = spreadsheet.add_worksheet(
                title=config["state_sheet_name"], rows=1000, cols=len(self.COLUMNS),
            )
        self._load()

    def _load(self):
        rows = self.worksheet.get("A:L", value_render_option="UNFORMATTED_VALUE")
        if not rows or not any(rows[0]):
            self.worksheet.update(range_name="A1:L1", values=[self.COLUMNS], raw=True)
            rows = [self.COLUMNS]
        if list(rows[0]) != self.COLUMNS:
            raise ValueError("The checkpoint worksheet has unexpected columns; choose a different state_sheet_name.")
        self.entries = {}
        for number, row in enumerate(rows[1:], start=2):
            entry = dict(zip(self.COLUMNS, (list(row) + [""] * len(self.COLUMNS))[:len(self.COLUMNS)]))
            key = str(entry["search_key"] or "")
            if not key:
                continue
            if key in self.entries:
                raise ValueError(f"Duplicate search checkpoint in row {number}.")
            entry["_row"] = number
            self.entries[key] = entry

    def _save(self, entry):
        values = [[entry.get(column, "") for column in self.COLUMNS]]
        if entry.get("_row"):
            row = entry["_row"]
            self.worksheet.update(range_name=f"A{row}:L{row}", values=values, raw=True)
            self.entries[entry["search_key"]] = dict(entry)
        else:
            self.worksheet.append_rows(values=values, value_input_option="RAW",
                                       insert_data_option="INSERT_ROWS", table_range="A1:L")
            self._load()

    def begin(self, settings):
        if settings.offset != 0:
            raise ValueError("Catch-up searches require offset: 0; a nonzero offset skips jobs.")
        if not settings.hours_old or settings.hours_old <= 0:
            raise ValueError("Catch-up searches require a positive hours_old.")
        identity = {
            "version": 1, "site": "linkedin", "jobs_sheet": self.jobs_sheet_name,
            "location": settings.location.strip().casefold(),
            "query": " ".join(settings.search_term.split()), "distance": settings.distance,
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = self.entries.get(key, {})
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=settings.hours_old)
        if previous.get("last_completed_search_at"):
            completed = parse_utc_timestamp(previous["last_completed_search_at"])
            if completed > now:
                raise ValueError("A search checkpoint is in the future; check its timestamp.")
            window_start = min(window_start, completed - timedelta(hours=self.config["overlap_hours"]))
        else:
            window_start = min(window_start, now - timedelta(hours=self.config["initial_lookback_hours"]))
        # A failed first attempt also keeps its original window across later runs.
        if previous.get("pending_window_start"):
            window_start = min(window_start, parse_utc_timestamp(previous["pending_window_start"]))
        hours = max(1, math.ceil((now - window_start).total_seconds() / 3600))
        effective = settings.model_copy(update={"hours_old": hours})
        progress = SearchProgress(effective, now, window_start, key)
        entry = {
            **previous, "search_key": key, "location": settings.location,
            "search_query": settings.search_term,
            "pending_window_start": format_utc_timestamp(window_start),
            "last_attempt_started_at": format_utc_timestamp(now), "last_attempt_finished_at": "",
            "last_attempt_status": "running", "last_attempt_hours_old": hours,
            "last_attempt_ids": 0, "last_attempt_pages": 0, "last_attempt_stop_reason": "",
        }
        self._save(entry)
        self.open_searches.append(progress)
        log_event(f"{settings.location}: searching since {format_utc_timestamp(window_start)}; "
                  f"lookback at start={hours}h; checkpoint={key[:12]}.")
        return progress

    def finish(self, progress, reason=None):
        if progress.finalized:
            return
        entry = dict(self.entries[progress.key])
        complete = progress.complete and progress.ids_saved == progress.ids_discovered
        entry.update(
            last_attempt_finished_at=utc_timestamp(),
            last_attempt_status="complete" if complete else "incomplete",
            last_attempt_ids=progress.ids_discovered, last_attempt_pages=progress.pages,
            last_attempt_stop_reason=reason or progress.stop_reason,
        )
        if complete:
            # Use search START time, so jobs arriving during a long run are covered next time.
            entry["last_completed_search_at"] = format_utc_timestamp(progress.started_at)
            entry["pending_window_start"] = ""
        self._save(entry)
        progress.finalized = True
        action = "advanced" if complete else "retained"
        log_event(f"{progress.settings.location}: discovery {entry['last_attempt_status']}; "
                  f"IDs={progress.ids_discovered}; pages={progress.pages}; "
                  f"reason={entry['last_attempt_stop_reason']}; checkpoint {action}.")

    def close_incomplete(self, reason):
        for progress in self.open_searches:
            if not progress.finalized:
                self.finish(progress, reason if progress.stop_reason == "not_finished" else None)


class QuotaAwareSheetsClient(HTTPClient):
    """Pace Sheets calls and retry explicit quota rejections, including appends."""

    MAX_CALLS_PER_MINUTE = 50  # Leave headroom below the standard per-user quota.
    MAX_QUOTA_RETRIES = 6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._calls = {"read": deque(), "write": deque()}
        self.request_counts = {"read": 0, "write": 0}

    def _wait_for_slot(self, kind):
        calls = self._calls[kind]
        while True:
            now = time.monotonic()
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) < self.MAX_CALLS_PER_MINUTE:
                calls.append(now)
                return
            delay = max(0.05, calls[0] + 60.05 - now)
            log_event(f"Google Sheets {kind} pacing: waiting {delay:.2f}s for a quota slot.")
            time.sleep(delay)

    def request(self, method, *args, **kwargs):
        kind = "read" if method.lower() in ("get", "head") else "write"
        for attempt in range(self.MAX_QUOTA_RETRIES + 1):
            self._wait_for_slot(kind)
            self.request_counts[kind] += 1
            log_event(f"Google Sheets request: {kind} #{self.request_counts[kind]}; "
                      f"method={method.upper()}; calls_in_last_60s={len(self._calls[kind])}; "
                      f"attempt={attempt + 1}.")
            try:
                return super().request(method, *args, **kwargs)
            except APIError as error:
                # Do not replay appends after ambiguous network/server errors.
                # An explicit 429 is a rejected request, safe to retry.
                if error.response.status_code != 429 or attempt == self.MAX_QUOTA_RETRIES:
                    raise
                delay = min(32, 2 ** attempt) + random.random()
                try:
                    delay = max(delay, float(error.response.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                log_event(f"Google Sheets quota (HTTP 429): waiting {delay:.1f}s before retry "
                          f"{attempt + 1}/{self.MAX_QUOTA_RETRIES}.")
                time.sleep(delay)


class SheetWriter:
    """Coalesce changed rows; keep each API payload comfortably below 2 MB."""

    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.pending = {}
        self.batch_requests = 0

    def queue(self, record):
        ensure_record_identity(record)
        row = record["_row"]
        self.pending[row] = {
            "range": f"A{row}:{LAST_COLUMN}{row}",
            "values": [[record.get(key, "") for key in COLUMNS]],
        }

    def flush(self):
        while self.pending:
            updates = []
            rows = []
            payload_bytes = 0
            for row, update in self.pending.items():
                size = len(json.dumps(update, ensure_ascii=False).encode("utf-8"))
                if updates and (len(updates) >= 50 or payload_bytes + size > 1_000_000):
                    break
                rows.append(row)
                updates.append(update)
                payload_bytes += size
            self.worksheet.batch_update(updates, raw=True)
            self.batch_requests += 1
            log_event(f"Sheets batch saved: {len(rows)} updated rows.")
            # Remove queued rows only after the server confirms the write.
            for row in rows:
                del self.pending[row]


def load_settings():
    with Path(__file__).with_name("search.yaml").open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("search.yaml must contain key: value settings.")
    config = dict(config)
    batch_size = config.pop("batch_size", 10)
    delay = config.pop("request_delay_seconds", 5)
    sheet_name = config.pop("sheet_name", "Jobs_AI")
    copy_cached_from = config.pop("copy_cached_from", None)
    if copy_cached_from is not None and (not isinstance(copy_cached_from, str) or not copy_cached_from.strip() or copy_cached_from == sheet_name):
        raise ValueError("copy_cached_from must name a different source worksheet.")
    llm = config.pop("llm")
    if "locations" in config and "location" in config:
        raise ValueError("Use locations or location in search.yaml, not both.")
    if "locations" in config:
        locations = config.pop("locations")
    elif "location" in config:
        locations = [config.pop("location")]
    else:
        raise ValueError("Missing setting: locations")
    if not isinstance(locations, list) or not locations or any(
        not isinstance(location, str) or not location.strip() for location in locations
    ):
        raise ValueError("locations must be a nonempty list of location names.")
    # Preserve the chosen order while avoiding repeated searches for identical names.
    unique_locations = {}
    for location in locations:
        location = location.strip()
        unique_locations.setdefault(location.casefold(), location)
    locations = list(unique_locations.values())
    filters = OUTPUT.filters
    keyword_filter = KeywordFilter.from_yaml(CONFIG_DIR / "keyword_filter.yaml")
    title_filter = TitleFilter.from_yaml(CONFIG_DIR / "title_filter.yaml")
    language_gate = {
        "enabled": True,
        "reject_languages": ["nl"],
        "minimum_score": 0.90,
        "minimum_letters": 200,
    }
    overrides = config.pop("language_gate", {})
    if not isinstance(overrides, dict) or set(overrides) - set(language_gate):
        raise ValueError("Invalid language_gate settings.")
    language_gate.update(overrides)
    if type(language_gate["enabled"]) is not bool:
        raise ValueError("language_gate.enabled must be true or false.")
    languages = language_gate["reject_languages"]
    if not isinstance(languages, list) or not languages or "en" in languages or any(
        not isinstance(value, str) or len(value) not in (2, 3) for value in languages
    ):
        raise ValueError('language_gate.reject_languages must contain non-English codes such as "nl".')
    minimum_score = language_gate["minimum_score"]
    if isinstance(minimum_score, bool) or not isinstance(minimum_score, (int, float)) or not 0.5 <= minimum_score <= 1:
        raise ValueError("language_gate.minimum_score must be between 0.5 and 1.")
    minimum_letters = language_gate["minimum_letters"]
    if type(minimum_letters) is not int or not 50 <= minimum_letters <= 5000:
        raise ValueError("language_gate.minimum_letters must be between 50 and 5000.")
    if type(batch_size) is not int or batch_size not in (10, 20):
        raise ValueError("batch_size must be 10 or 20.")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 60:
        raise ValueError("request_delay_seconds must be between 0 and 60.")
    if not isinstance(sheet_name, str) or not sheet_name.strip():
        raise ValueError("sheet_name must be a nonempty worksheet name.")
    if not isinstance(llm, dict) or not isinstance(llm.get("model"), str) or not llm["model"]:
        raise ValueError("llm.model must specify an OpenAI model ID.")
    if set(llm) - {"model", "reasoning_effort", "max_output_tokens"}:
        raise ValueError("Unknown llm setting.")
    if type(llm.get("max_output_tokens", 512)) is not int or not 256 <= llm.get("max_output_tokens", 512) <= 4096:
        raise ValueError("llm.max_output_tokens must be between 256 and 4096.")
    allowed = {"search_term", "distance", "hours_old", "results_wanted", "offset"}
    if set(config) - allowed:
        raise ValueError(f"Unknown search settings: {set(config) - allowed}")
    for name in ("search_term", "hours_old", "results_wanted"):
        if name not in config:
            raise ValueError(f"Missing setting: {name}")
    searches = [ScraperInput(
        site_name=["linkedin"], fetch_description=False, location=location,
        description_format="plain", verbose=2, **config,
    ) for location in locations]
    if any(settings.results_wanted > max_results or settings.offset >= max_results
           for settings in searches):
        raise ValueError(f"Search limit/offset must stay within JobSpy's {max_results} result ceiling.")
    return searches, batch_size, float(delay), sheet_name, llm, filters, language_gate, keyword_filter, copy_cached_from, title_filter


def rejected_description_language(description, config):
    """Only clear matches in substantial descriptions bypass the LLM.

    Detector scores are model outputs, not calibrated accuracy percentages.
    Short, uncertain and unrecognized text continues to the LLM.
    """
    if not config["enabled"]:
        return None
    if sum(character.isalpha() for character in description) < config["minimum_letters"]:
        return None
    try:
        candidates = detect_langs(description)
    except LangDetectException:
        return None
    if candidates:
        best = candidates[0]
        if best.lang in config["reject_languages"] and best.prob >= config["minimum_score"]:
            return best.lang, float(best.prob)
    return None


def validate_analysis(result):
    return OUTPUT.validate(result)


def matches_filters(result, filters):
    return all(result[field] in filters[field] for field in ENUMS)


class AnalysisError(RuntimeError):
    pass


class Analyzer:
    def __init__(self, config):
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ValueError("Add OPENAI_API_KEY as a GitHub Actions repository secret.")
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        })

    def analyze(self, record):
        payload = {
            "model": self.config["model"],
            "instructions": PROMPT,
            "input": json.dumps({
                key: record.get(key, "") for key in ("name", "company", "location", "description")
            }, ensure_ascii=False),
            "text": {"format": {
                "type": "json_schema", "name": "job_classification", "strict": True,
                "schema": SCHEMA,
            }},
            "max_output_tokens": self.config.get("max_output_tokens", 512),
            "store": False,
        }
        if self.config.get("reasoning_effort"):
            payload["reasoning"] = {"effort": self.config["reasoning_effort"]}
        try:
            response = self.session.post(
                "https://api.openai.com/v1/responses", json=payload,
                timeout=60, allow_redirects=False,
            )
        except requests.RequestException:
            raise AnalysisError("OpenAI connection/timeout error; cached description retained.") from None
        if response.status_code != 200:
            # Never include raw HTTP bodies or credentials in logs or Sheets.
            raise AnalysisError(f"OpenAI HTTP {response.status_code}; check API access, credits and model ID.")
        try:
            body = response.json()
            if body.get("status") != "completed":
                raise ValueError("Response did not complete; check the output token limit.")
            texts = []
            for item in body.get("output", []):
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        raise ValueError("The model refused to classify this posting.")
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            result = validate_analysis(json.loads("".join(texts)))
        except (ValueError, KeyError, TypeError):
            raise AnalysisError("OpenAI returned no valid completed classification; cached description retained.") from None
        usage = body.get("usage", {})
        log_event(f"LLM tokens: input={usage.get('input_tokens', '?')}, output={usage.get('output_tokens', '?')}", record)
        return result

    def close(self):
        self.session.close()


def read_jobs(worksheet):
    rows = worksheet.get(f"A:{LAST_COLUMN}", value_render_option="UNFORMATTED_VALUE")
    if rows and any(value != "" for value in rows[0]):
        header = list(rows[0])
        while header and header[-1] == "":
            header.pop()
        # The sole automatic schema upgrade appends record_id; existing columns stay put.
        if header not in (COLUMNS, COLUMNS[:-1]):
            raise ValueError("The worksheet uses another schema. Choose a new sheet_name in search.yaml if you changed the output fields.")
    records = {}
    for number, row in enumerate(rows[1:], start=2):
        values = (list(row) + [""] * len(COLUMNS))[:len(COLUMNS)]
        record = dict(zip(COLUMNS, values))
        url = str(record["job_url"] or "").strip()
        if not url:
            continue
        if url in records:
            raise ValueError(f"Duplicate job URL in row {number}.")
        if record["analysis_status"] not in ("pending_description", "pending_analysis", "done", "needs_review", "skipped_language", "skipped_keywords", "skipped_title"):
            raise ValueError(f"Invalid analysis_status in row {number}.")
        if record["analysis_status"] == "done":
            validate_analysis({key: record[key] for key in OUTPUT.fields})
        record["job_url"] = url
        record["_row"] = number
        if ensure_record_identity(record):
            record["_identity_dirty"] = True
        records[url] = record
    return records


def write_record(writer, record):
    writer.queue(record)


def save_batch(worksheet, records, jobs, settings, writer):
    rows = []
    for job in jobs:
        url = job["job_url"]
        if url in records:
            record = records[url]
            changed = False
            for key in ("name", "company", "location", "date_posted"):
                if not record[key] and job.get(key):
                    record[key] = job[key].isoformat() if isinstance(job[key], date) else job[key]
                    changed = True
            if changed:
                write_record(writer, record)
            continue
        posted = job.get("date_posted")
        record = {
            **job,
            "date_posted": posted.isoformat() if posted else "",
            "analysis_status": "pending_description",
            "first_seen_at": utc_timestamp(),
            "search_query": settings.search_term,
        }
        ensure_record_identity(record)
        rows.append([record.get(key, "") for key in COLUMNS])
    # Commit changed metadata before reloading the worksheet after an append.
    writer.flush()
    if rows:
        worksheet.append_rows(
            values=rows, value_input_option="RAW",
            insert_data_option="INSERT_ROWS", table_range=f"A1:{LAST_COLUMN}",
        )
        records = read_jobs(worksheet)
    return records, len(rows)


def import_cached_jobs(spreadsheet, worksheet, records, source_name):
    """Reuse descriptions after changing the output columns; old classifications are not reused."""
    try:
        source = spreadsheet.worksheet(source_name)
    except gspread.WorksheetNotFound:
        return records
    rows = source.get("A:AZ", value_render_option="UNFORMATTED_VALUE")
    if not rows or "job_url" not in rows[0] or "description" not in rows[0]:
        return records
    seen = set(records)
    additions = []
    for values in rows[1:]:
        old = dict(zip(rows[0], values))
        url = str(old.get("job_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        record = {key: old.get(key, "") for key in
                  META_BEFORE + ["job_url", "date_posted", "description", "first_seen_at", "search_query", "record_id"]}
        record["job_url"] = url
        record.update(OUTPUT.blank_result())
        record["analysis_status"] = "pending_analysis" if record["description"] else "pending_description"
        ensure_record_identity(record)
        additions.append([record.get(key, "") for key in COLUMNS])
    for start in range(0, len(additions), 50):
        worksheet.append_rows(values=additions[start:start + 50], value_input_option="RAW",
                              insert_data_option="INSERT_ROWS", table_range=f"A1:{LAST_COLUMN}")
    if additions:
        log_event(f"Imported {len(additions)} cached jobs from {source_name}; language requirements will be rechecked.")
        records = read_jobs(worksheet)
    return records


def apply_title_filter(writer, record, title_filter):
    """Exclude titles before description requests; retain any existing paid analysis."""
    accepted, reason = title_filter.evaluate(record["name"])
    updates = {}
    if not accepted:
        updates["matches_filters"] = False
        # Preserve completed classifications so editing the filter needs no paid retry.
        if record["analysis_status"] != "done":
            updates.update(analysis_status="skipped_title", analysis_error=reason)
    elif record["analysis_status"] == "skipped_title":
        # Removing a title rule reactivates its saved metadata/description.
        updates.update(
            analysis_status="pending_analysis" if record["description"] else "pending_description",
            matches_filters="", analysis_error="",
        )
    if any(record.get(key) != value for key, value in updates.items()):
        record.update(updates)
        write_record(writer, record)
        if not accepted:
            log_event(f"Skipped by title: {record['name']}. {reason}", record)
    return accepted


def process_batch(writer, records, urls, scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter):
    try:
        return _process_batch(writer, records, urls, scraper, analyzer, filters, attempted,
                              language_gate, keyword_filter, title_filter)
    finally:
        # Includes title/language/keyword rejections and early stops.
        writer.flush()


def _process_batch(writer, records, urls, scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter):
    completed = 0
    for url in urls:
        if url in attempted:
            continue
        record = records[url]
        if not apply_title_filter(writer, record, title_filter):
            attempted.add(url)
            continue
        if record["analysis_status"] in ("done", "needs_review"):
            continue
        attempted.add(url)
        if not record["description"]:
            if scraper.monitor.blocked:
                return completed, False
            try:
                job_id = linkedin_job_id(url)
            except ValueError:
                scraper.log.warning(f"Invalid job URL in row {record['_row']}.")
                continue
            scraper.wait_before_request()
            log_event(f"Downloading description: {job_id}", record)
            try:
                details = scraper._fetch_details(job_id)
                description = details.get("description") or ""
            except Exception:
                description = ""
            if not isinstance(description, str) or not description.strip():
                record["analysis_error"] = "Description unavailable; left pending for retry."
                write_record(writer, record)
                if scraper.monitor.blocked:
                    return completed, False
                continue
            if len(description.encode("utf-16-le")) // 2 > 48000:
                record["analysis_status"] = "needs_review"
                record["analysis_error"] = "Description exceeds the cache-cell limit; review manually."
                write_record(writer, record)
                continue
            record["description"] = description
            record["analysis_status"] = "pending_analysis"
            record["analysis_error"] = ""
            write_record(writer, record)
        # Matching rules live in keyword_filter.yaml; config_engine.py interprets them.
        accepted, reason = keyword_filter.evaluate(record["description"])
        if not accepted:
            note = "Skipped before LLM: " + reason
            skipped_values = {
                **OUTPUT.blank_result(),
                "matches_filters": False, "analysis_status": "skipped_keywords",
                "analysis_error": note, "analysis_model": "",
            }
            changed = any(record.get(key) != value for key, value in skipped_values.items())
            record.update(skipped_values)
            if changed:
                write_record(writer, record)
            log_event(f"{note} Job: {record['name']}", record)
            continue
        rejection = rejected_description_language(record["description"], language_gate)
        if rejection is not None:
            language, score = rejection
            note = f"Skipped before LLM: description language {language}; detector score {score:.4f}."
            # Dutch/experience stay blank: they were never analyzed, so 'na' would be misleading.
            skipped_values = {
                **OUTPUT.blank_result(language_rejected=True),
                "matches_filters": False, "analysis_status": "skipped_language",
                "analysis_error": note, "analysis_model": "",
            }
            changed = any(record.get(key) != value for key, value in skipped_values.items())
            record.update(skipped_values)
            if changed:
                write_record(writer, record)
            log_event(f"{note} Job: {record['name']}", record)
            continue
        if record["analysis_status"] in ("skipped_language", "skipped_keywords"):
            # Changing/disabling a local filter can reactivate a cached description.
            record.update({
                **OUTPUT.blank_result(),
                "matches_filters": "", "analysis_status": "pending_analysis", "analysis_error": "",
            })
            write_record(writer, record)
        # Persist descriptions before spending tokens, including queued rejections.
        writer.flush()
        if getattr(analyzer, "paused", False):
            record["analysis_status"] = "pending_analysis"
            record["analysis_error"] = "LLM paused after an earlier error; retry on the next run."
            write_record(writer, record)
            log_event(f"Queued for later LLM analysis: {record['name']}", record)
            continue
        log_event(f"Classifying: {record['name']}", record)
        try:
            result = analyzer.analyze(record)
        except AnalysisError as error:
            analyzer.paused = True
            record["analysis_status"] = "pending_analysis"
            record["analysis_error"] = str(error)
            write_record(writer, record)
            scraper.log.error(str(error))
            # Discovery and description caching can continue without paid API calls.
            continue
        record.update(result)
        record["matches_filters"] = matches_filters(result, filters)
        record["analysis_status"] = "done"
        record["analysis_error"] = ""
        record["analysis_model"] = analyzer.config["model"]
        write_record(writer, record)
        # Retain completed paid classifications even if a later job fails.
        writer.flush()
        completed += 1
    return completed, not scraper.monitor.blocked


class SearchMonitor(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.blocked = False

    def emit(self, record):
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            self.messages.append(message)
        if any(f"status code {code}" in message for code in (403, 429)):
            self.blocked = True


class Collector(LinkedIn):
    def __init__(self, settings, monitor, request_delay):
        super().__init__()
        self.scraper_input = settings
        self.monitor = monitor
        self.request_delay = request_delay
        self.last_request = None
        self.session = create_session(
            RequestsRotating(settings.proxies, clear_cookies=True),
            settings,
            headers,
        )

    def wait_before_request(self):
        if self.last_request is not None:
            remaining = self.request_delay - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request = time.monotonic()

    def search_batches(self, batch_size, progress=None):
        """Yield metadata batches, recording observed completion independently of analysis."""
        settings = self.scraper_input
        if progress is None:
            now = datetime.now(timezone.utc)
            progress = SearchProgress(settings, now, now - timedelta(hours=settings.hours_old))
        seen = set()
        batch = []
        pages_in_batch = 0
        params = self._search_params()
        invalid_metadata = False

        if settings.offset:
            self.log.warning("Nonzero offset skips the beginning of the search.")

        for start in range(settings.offset, max_results, jobs_per_page):
            if self.monitor.blocked:
                progress.stop_reason = "linkedin_requests_stopped"
                break
            self.wait_before_request()
            # Keep the oldest requested instant fixed even while descriptions take time.
            elapsed = (datetime.now(timezone.utc) - progress.window_start).total_seconds()
            page_params = {**params, "start": start, "f_TPR": f"r{max(1, math.ceil(elapsed))}"}
            self.log.info(f"{settings.location}: search page at offset {start}")
            response = None
            try:
                response = self.session.get(
                    f"{self.base_url}/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params=page_params,
                )
                check_status(response)
            except Exception as error:
                status = response.status_code if response is not None else None
                progress.stop_reason = f"http_{status}" if status else "request_error"
                self.log.error(f"Search interrupted: {error}")
                break

            progress.pages += 1
            cards = BeautifulSoup(response.text, "html.parser").find_all(class_="base-search-card")
            if not cards:
                if response.text.strip() == empty_page.strip():
                    progress.complete = not invalid_metadata and settings.offset == 0
                    progress.stop_reason = "standard_empty_results_page" if progress.complete else "incomplete_metadata"
                    self.log.info(f"{settings.location}: LinkedIn returned its standard empty results page.")
                else:
                    progress.stop_reason = "unexpected_empty_html"
                    self.log.error("Unexpected search page with no jobs; search stopped.")
                break

            pages_in_batch += 1
            reached_limit = False
            for card in cards:
                job_id = str(card.get("data-entity-urn", "")).rsplit(":", 1)[-1]
                if not job_id.isdigit():
                    invalid_metadata = True
                    self.log.warning("A search card has no readable job ID.")
                    continue
                if job_id in seen:
                    continue
                seen.add(job_id)
                progress.ids_discovered = len(seen)
                title_tag = card.find("h3", class_="base-search-card__title")
                company_tag = card.find("h4", class_="base-search-card__subtitle")
                location_tag = card.find("span", class_="job-search-card__location")
                time_tag = card.find("time")
                posted = None
                if time_tag is not None:
                    try:
                        posted = date.fromisoformat(str(time_tag.get("datetime", "")))
                    except ValueError:
                        pass
                batch.append({
                    "name": title_tag.get_text(strip=True) if title_tag else "",
                    "location": location_tag.get_text(strip=True) if location_tag else "",
                    "company": company_tag.get_text(strip=True) if company_tag else "",
                    "job_url": f"{self.base_url}/jobs/view/{job_id}",
                    "date_posted": posted,
                })
                if len(seen) >= settings.results_wanted:
                    reached_limit = True
                    break

            terminal = reached_limit or len(cards) < jobs_per_page
            if reached_limit:
                progress.stop_reason = "configured_result_limit"
                self.log.warning("Configured result limit reached; more jobs may exist.")
            elif terminal:
                progress.complete = not invalid_metadata and settings.offset == 0
                progress.stop_reason = "short_results_page" if progress.complete else "incomplete_metadata"

            while len(batch) >= batch_size:
                yield batch[:batch_size]
                batch = batch[batch_size:]
                pages_in_batch = 0
                if self.monitor.blocked:
                    # A completed last page is still checkpointable if its IDs were saved.
                    if not progress.complete:
                        progress.stop_reason = "linkedin_requests_stopped"
                    return
            if batch and (terminal or pages_in_batch >= batch_size // jobs_per_page):
                yield batch
                batch = []
                pages_in_batch = 0
                if self.monitor.blocked:
                    if not progress.complete:
                        progress.stop_reason = "linkedin_requests_stopped"
                    return
            if terminal:
                break
        else:
            progress.stop_reason = "pagination_ceiling"
            self.log.warning("JobSpy pagination ceiling reached; more jobs may exist.")

        if batch:
            yield batch


def search_locations(scraper, searches, batch_size, checkpoints=None):
    """Interleave locations; acknowledge discovery only after the caller saves each batch."""
    active = [(settings, None, None) for settings in searches]
    while active and not scraper.monitor.blocked:
        remaining = []
        for settings, progress, batches in active:
            if scraper.monitor.blocked:
                return
            if progress is None:
                if checkpoints is not None:
                    progress = checkpoints.begin(settings)
                else:
                    now = datetime.now(timezone.utc)
                    progress = SearchProgress(settings, now, now - timedelta(hours=settings.hours_old))
                batches = scraper.search_batches(batch_size, progress)
            scraper.scraper_input = progress.settings
            try:
                jobs = next(batches)
            except StopIteration:
                if checkpoints is not None:
                    checkpoints.finish(progress)
                continue
            yield progress, jobs
            remaining.append((settings, progress, batches))
        active = remaining


def main():
    searches, batch_size, delay, sheet_name, llm, filters, language_gate, keyword_filter, copy_cached_from, title_filter = load_settings()
    catchup = load_catchup_settings(sheet_name)
    mode = os.environ.get("COLLECTOR_MODE", "all")
    if mode not in ("all", "descriptions"):
        raise ValueError("COLLECTOR_MODE must be all or descriptions.")
    analyzer = Analyzer(llm)
    scraper = None
    checkpoints = None
    monitor = SearchMonitor()
    try:
        client = gspread.service_account_from_dict(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            http_client=QuotaAwareSheetsClient,
        )
        spreadsheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(COLUMNS)))
        records = read_jobs(worksheet)
        worksheet.update(range_name=f"A1:{LAST_COLUMN}1", values=[COLUMNS], raw=True)
        worksheet.freeze(rows=1)
        # Keep the new ID visible; only description/cache/provenance columns are hidden.
        worksheet.hide_columns(COLUMNS.index("description"), COLUMNS.index("record_id"))
        if copy_cached_from:
            records = import_cached_jobs(spreadsheet, worksheet, records, copy_cached_from)
        writer = SheetWriter(worksheet)
        # Apply changed filters to cached results without spending more model tokens.
        backfilled = 0
        for record in records.values():
            if record.pop("_identity_dirty", False):
                write_record(writer, record)
                backfilled += 1
            if not apply_title_filter(writer, record, title_filter):
                continue
            if record["analysis_status"] == "done":
                eligible = matches_filters(record, filters) and keyword_filter.matches(record["description"])
                if record["matches_filters"] != eligible:
                    record["matches_filters"] = eligible
                    write_record(writer, record)
        writer.flush()
        if backfilled:
            log_event(f"Added record_id to {backfilled} cached jobs using their first_seen_at timestamps.")
        old_pending = [url for url, record in records.items()
                       if record["analysis_status"] in ("pending_description", "pending_analysis", "skipped_language", "skipped_keywords", "skipped_title")]
        old_position = 0
        attempted = set()
        completed = 0
        saved = 0
        running = True
        scraper = Collector(searches[0], monitor, delay)
        scraper.log.setLevel(logging.INFO)
        scraper.log.addHandler(monitor)
        log_event(f"Sheet: {sheet_name}; model: {llm['model']}; mode: {mode}")
        if mode == "all":
            if catchup["enabled"]:
                checkpoints = CheckpointStore(spreadsheet, sheet_name, catchup)
            log_event(f"Searching {len(searches)} locations; base lookback: {searches[0].hours_old} hours; "
                      f"catch-up: {catchup['enabled']}; batch: {batch_size}")
            for progress, jobs in search_locations(scraper, searches, batch_size, checkpoints):
                settings = progress.settings
                records, added = save_batch(worksheet, records, jobs, settings, writer)
                progress.ids_saved += len(jobs)
                saved += added
                # Pending descriptions/LLM results cannot undo a completed, durable ID search.
                if checkpoints is not None and progress.complete and progress.ids_saved == progress.ids_discovered:
                    checkpoints.finish(progress)
                count, running = process_batch(
                    writer, records, [job["job_url"] for job in jobs],
                    scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
                )
                completed += count
                log_event(f"{settings.location}: batch saved: {added} new jobs; {count} classified.")
                if not running:
                    break
                count, running = process_batch(
                    writer, records, old_pending[old_position:old_position + batch_size],
                    scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
                )
                completed += count
                old_position += batch_size
                if not running:
                    break
        while running and old_position < len(old_pending):
            count, running = process_batch(
                writer, records, old_pending[old_position:old_position + batch_size],
                scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
            )
            completed += count
            old_position += batch_size
        if checkpoints is not None:
            checkpoints.close_incomplete("linkedin_requests_stopped" if monitor.blocked else "run_stopped")
        pending = sum(record["analysis_status"] not in ("done", "skipped_language", "skipped_keywords", "skipped_title") for record in records.values())
        skipped = sum(record["analysis_status"] == "skipped_language" for record in records.values())
        skipped_keywords = sum(record["analysis_status"] == "skipped_keywords" for record in records.values())
        skipped_titles = sum(not title_filter.matches(record["name"]) for record in records.values())
        eligible = sum(record["analysis_status"] == "done" and record["matches_filters"] is True
                       for record in records.values())
        log_event(f"New jobs: {saved}; classified: {completed}; pending/review: {pending}; skipped titles: {skipped_titles}; skipped language: {skipped}; skipped keywords: {skipped_keywords}; matching filters: {eligible}")
        log_event(f"Sheets: {writer.batch_requests} batched row-update requests.")
        if getattr(analyzer, "paused", False):
            log_event("LLM classification paused; saved descriptions remain pending for the next run.")
        for message in dict.fromkeys(monitor.messages):
            log_event(f"ATTENTION: {message}")
        if monitor.blocked:
            log_event("LinkedIn returned 403/429; further LinkedIn requests stopped.")
        return 1 if pending or monitor.messages or not running else 0
    finally:
        analyzer.close()
        if scraper is not None:
            scraper.log.removeHandler(monitor)
            scraper.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
