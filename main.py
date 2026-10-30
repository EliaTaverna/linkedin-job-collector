"""LinkedIn metadata -> cached descriptions -> structured LLM analysis -> Sheets."""

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import gspread
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

logging.basicConfig(level=logging.INFO)
DetectorFactory.seed = 0

CONFIG_DIR = Path(__file__).resolve().parent
OUTPUT = LLMOutput.from_yaml(CONFIG_DIR / "llm_output.yaml")
META_BEFORE = ["name", "company", "location"]
META_AFTER = [
    "job_url", "date_posted", "matches_filters", "analysis_status",
    "description", "analysis_error", "first_seen_at", "search_query", "analysis_model",
]
if set(OUTPUT.fields) & set(META_BEFORE + META_AFTER):
    raise ValueError("LLM output field names cannot overwrite metadata/cache columns.")
COLUMNS = META_BEFORE + OUTPUT.fields + META_AFTER
LAST_COLUMN = column_name(len(COLUMNS))
ENUMS = OUTPUT.enums
SCHEMA = OUTPUT.schema
PROMPT = OUTPUT.instructions


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
        print(f"LLM tokens: input={usage.get('input_tokens', '?')}, output={usage.get('output_tokens', '?')}", flush=True)
        return result

    def close(self):
        self.session.close()


def read_jobs(worksheet):
    rows = worksheet.get(f"A:{LAST_COLUMN}", value_render_option="UNFORMATTED_VALUE")
    if rows and any(value != "" for value in rows[0]):
        if list(rows[0][:len(COLUMNS)]) != COLUMNS:
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
        records[url] = record
    return records


def write_record(worksheet, record):
    row = record["_row"]
    worksheet.update(
        range_name=f"A{row}:{LAST_COLUMN}{row}",
        values=[[record.get(key, "") for key in COLUMNS]], raw=True,
    )


def save_batch(worksheet, records, jobs, settings):
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
                write_record(worksheet, record)
            continue
        posted = job.get("date_posted")
        record = {
            **job,
            "date_posted": posted.isoformat() if posted else "",
            "analysis_status": "pending_description",
            "first_seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "search_query": settings.search_term,
        }
        rows.append([record.get(key, "") for key in COLUMNS])
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
                  META_BEFORE + ["job_url", "date_posted", "description", "first_seen_at", "search_query"]}
        record["job_url"] = url
        record.update(OUTPUT.blank_result())
        record["analysis_status"] = "pending_analysis" if record["description"] else "pending_description"
        additions.append([record.get(key, "") for key in COLUMNS])
    for start in range(0, len(additions), 50):
        worksheet.append_rows(values=additions[start:start + 50], value_input_option="RAW",
                              insert_data_option="INSERT_ROWS", table_range=f"A1:{LAST_COLUMN}")
    if additions:
        print(f"Imported {len(additions)} cached jobs from {source_name}; language requirements will be rechecked.", flush=True)
        records = read_jobs(worksheet)
    return records


def apply_title_filter(worksheet, record, title_filter):
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
        write_record(worksheet, record)
        if not accepted:
            print(f"Skipped by title: {record['name']}. {reason}", flush=True)
    return accepted


def process_batch(worksheet, records, urls, scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter):
    completed = 0
    for url in urls:
        if url in attempted:
            continue
        record = records[url]
        if not apply_title_filter(worksheet, record, title_filter):
            attempted.add(url)
            continue
        if record["analysis_status"] in ("done", "needs_review"):
            continue
        attempted.add(url)
        if not record["description"]:
            if scraper.monitor.blocked:
                return completed, False
            job_id = url.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit():
                scraper.log.warning(f"Invalid job URL in row {record['_row']}.")
                continue
            scraper.wait_before_request()
            print(f"Downloading description: {job_id}", flush=True)
            try:
                details = scraper._fetch_details(job_id)
                description = details.get("description") or ""
            except Exception:
                description = ""
            if not isinstance(description, str) or not description.strip():
                record["analysis_error"] = "Description unavailable; left pending for retry."
                write_record(worksheet, record)
                if scraper.monitor.blocked:
                    return completed, False
                continue
            if len(description.encode("utf-16-le")) // 2 > 48000:
                record["analysis_status"] = "needs_review"
                record["analysis_error"] = "Description exceeds the cache-cell limit; review manually."
                write_record(worksheet, record)
                continue
            record["description"] = description
            record["analysis_status"] = "pending_analysis"
            record["analysis_error"] = ""
            # Cache the source BEFORE the API call so retries need no LinkedIn download.
            write_record(worksheet, record)
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
                write_record(worksheet, record)
            print(f"{note} Job: {record['name']}", flush=True)
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
                write_record(worksheet, record)
            print(f"{note} Job: {record['name']}", flush=True)
            continue
        if record["analysis_status"] in ("skipped_language", "skipped_keywords"):
            # Changing/disabling a local filter can reactivate a cached description.
            record.update({
                **OUTPUT.blank_result(),
                "matches_filters": "", "analysis_status": "pending_analysis", "analysis_error": "",
            })
            write_record(worksheet, record)
        print(f"Classifying: {record['name']}", flush=True)
        try:
            result = analyzer.analyze(record)
        except AnalysisError as error:
            record["analysis_status"] = "pending_analysis"
            record["analysis_error"] = str(error)
            write_record(worksheet, record)
            scraper.log.error(str(error))
            return completed, False
        record.update(result)
        record["matches_filters"] = matches_filters(result, filters)
        record["analysis_status"] = "done"
        record["analysis_error"] = ""
        record["analysis_model"] = analyzer.config["model"]
        write_record(worksheet, record)
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

    def search_batches(self, batch_size):
        """Yield metadata batches; the caller finishes descriptions before resuming."""
        settings = self.scraper_input
        seen = set()
        batch = []
        pages_in_batch = 0
        params = self._search_params()
        reached_limit = False

        if settings.offset:
            self.log.warning("Nonzero offset skips the beginning of the search.")

        for start in range(settings.offset, max_results, jobs_per_page):
            if self.monitor.blocked:
                break
            self.wait_before_request()
            self.log.info(f"{settings.location}: search page at offset {start}")
            try:
                response = self.session.get(
                    f"{self.base_url}/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params={**params, "start": start},
                )
                check_status(response)
            except Exception as error:
                self.log.error(f"Search interrupted: {error}")
                break

            cards = BeautifulSoup(response.text, "html.parser").find_all(
                class_="base-search-card"
            )
            if not cards:
                if response.text.strip() != empty_page:
                    self.log.error("Unexpected search page with no jobs; search stopped.")
                elif start > settings.offset:
                    self.log.warning("Empty search page: end of results or throttling.")
                break

            pages_in_batch += 1
            for card in cards:
                job_id = str(card.get("data-entity-urn", "")).rsplit(":", 1)[-1]
                if not job_id.isdigit():
                    self.log.warning("A search card has no readable job ID.")
                    continue
                if job_id in seen:
                    continue
                seen.add(job_id)

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

                if len(batch) >= batch_size:
                    yield batch
                    batch = []
                    pages_in_batch = 0
                    if self.monitor.blocked:
                        return

                if len(seen) >= settings.results_wanted:
                    reached_limit = True
                    break

            # Flush a partial batch too; duplicates must not cause a large buildup.
            if batch and pages_in_batch >= batch_size // jobs_per_page:
                yield batch
                batch = []
                pages_in_batch = 0

            if reached_limit:
                self.log.warning("Configured result limit reached; more jobs may exist.")
                break
            if len(cards) < jobs_per_page:
                break
        else:
            self.log.warning("JobSpy pagination ceiling reached; more jobs may exist.")

        if batch:
            yield batch



def search_locations(scraper, searches, batch_size):
    """Take one metadata batch per location in turn, sharing one request delay."""
    active = [(settings, scraper.search_batches(batch_size)) for settings in searches]
    while active and not scraper.monitor.blocked:
        remaining = []
        for settings, batches in active:
            if scraper.monitor.blocked:
                return
            scraper.scraper_input = settings
            try:
                jobs = next(batches)
            except StopIteration:
                continue
            # The caller finishes this batch's descriptions before the next search.
            yield settings, jobs
            remaining.append((settings, batches))
        active = remaining


def main():
    searches, batch_size, delay, sheet_name, llm, filters, language_gate, keyword_filter, copy_cached_from, title_filter = load_settings()
    mode = os.environ.get("COLLECTOR_MODE", "all")
    if mode not in ("all", "descriptions"):
        raise ValueError("COLLECTOR_MODE must be all or descriptions.")
    analyzer = Analyzer(llm)
    scraper = None
    monitor = SearchMonitor()
    try:
        client = gspread.service_account_from_dict(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        spreadsheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(COLUMNS)))
        records = read_jobs(worksheet)
        worksheet.update(range_name=f"A1:{LAST_COLUMN}1", values=[COLUMNS], raw=True)
        worksheet.freeze(rows=1)
        worksheet.hide_columns(COLUMNS.index("description"), len(COLUMNS))  # Cache/provenance fields.
        if copy_cached_from:
            records = import_cached_jobs(spreadsheet, worksheet, records, copy_cached_from)
        # Apply changed filters to cached results without spending more model tokens.
        for record in records.values():
            if not apply_title_filter(worksheet, record, title_filter):
                continue
            if record["analysis_status"] == "done":
                eligible = matches_filters(record, filters) and keyword_filter.matches(record["description"])
                if record["matches_filters"] != eligible:
                    record["matches_filters"] = eligible
                    write_record(worksheet, record)
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
        print(f"Sheet: {sheet_name}; model: {llm['model']}; mode: {mode}", flush=True)
        if mode == "all":
            print(f"Searching {len(searches)} locations; lookback: {searches[0].hours_old} hours; batch: {batch_size}", flush=True)
            for settings, jobs in search_locations(scraper, searches, batch_size):
                records, added = save_batch(worksheet, records, jobs, settings)
                saved += added
                count, running = process_batch(
                    worksheet, records, [job["job_url"] for job in jobs],
                    scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
                )
                completed += count
                print(f"{settings.location}: batch saved: {added} new jobs; {count} classified.", flush=True)
                if not running:
                    break
                count, running = process_batch(
                    worksheet, records, old_pending[old_position:old_position + batch_size],
                    scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
                )
                completed += count
                old_position += batch_size
                if not running:
                    break
        while running and old_position < len(old_pending):
            count, running = process_batch(
                worksheet, records, old_pending[old_position:old_position + batch_size],
                scraper, analyzer, filters, attempted, language_gate, keyword_filter, title_filter,
            )
            completed += count
            old_position += batch_size
        pending = sum(record["analysis_status"] not in ("done", "skipped_language", "skipped_keywords", "skipped_title") for record in records.values())
        skipped = sum(record["analysis_status"] == "skipped_language" for record in records.values())
        skipped_keywords = sum(record["analysis_status"] == "skipped_keywords" for record in records.values())
        skipped_titles = sum(not title_filter.matches(record["name"]) for record in records.values())
        eligible = sum(record["analysis_status"] == "done" and record["matches_filters"] is True
                       for record in records.values())
        print(f"New jobs: {saved}; classified: {completed}; pending/review: {pending}; skipped titles: {skipped_titles}; skipped language: {skipped}; skipped keywords: {skipped_keywords}; matching filters: {eligible}", flush=True)
        for message in dict.fromkeys(monitor.messages):
            print(f"ATTENTION: {message}", flush=True)
        if monitor.blocked:
            print("LinkedIn returned 403/429; further LinkedIn requests stopped.", flush=True)
        return 1 if pending or monitor.messages or not running else 0
    finally:
        analyzer.close()
        if scraper is not None:
            scraper.log.removeHandler(monitor)
            scraper.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
