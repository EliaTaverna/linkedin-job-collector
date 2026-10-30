import json
import logging
import os
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

import gspread
import yaml
from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.constant import empty_page, headers, jobs_per_page, max_results
from jobspy.model import ScraperInput
from jobspy.util import RequestsRotating, check_status, create_session
from langdetect import DetectorFactory, PROFILES_DIRECTORY, detect
from langdetect.lang_detect_exception import LangDetectException


logging.basicConfig(level=logging.INFO)
DetectorFactory.seed = 0

COLUMNS = [
    "title",
    "company",
    "job_url",
    "mentions_dutch",
    "date_posted",
    "ad_language",
    "description_status",
    "english likelihood title",
]

SKIPPED_TITLE_STATUS = "skipped_title_language"
_title_factory = None


def load_settings():
    path = Path(__file__).resolve().with_name("search.yaml")

    with path.open(encoding="utf-8") as config_file:
        parameters = yaml.safe_load(config_file)

    if not isinstance(parameters, dict):
        raise ValueError("search.yaml must contain key: value settings.")

    minimum = parameters.pop("min_english_likelihood_title", 0.50)
    batch_size = parameters.pop("batch_size", 10)
    request_delay = parameters.pop("request_delay_seconds", 5)
    if type(batch_size) is not int or batch_size not in (10, 20):
        raise ValueError("batch_size must be 10 or 20.")
    if (
        isinstance(request_delay, bool)
        or not isinstance(request_delay, (int, float))
        or not 0 <= request_delay <= 60
    ):
        raise ValueError("request_delay_seconds must be between 0 and 60.")

    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not 0 <= minimum <= 1
    ):
        raise ValueError(
            "min_english_likelihood_title must be between 0 and 1; "
            "0.50 means 50%."
        )

    allowed = {
        "search_term", "location", "results_wanted",
        "distance", "hours_old", "offset",
    }

    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(f"Unknown search.yaml settings: {unknown}")

    for name in ("search_term", "location", "results_wanted", "hours_old"):
        if name not in parameters:
            raise ValueError(f"Missing search.yaml setting: {name}")

    settings = ScraperInput(
        site_name=["linkedin"],
        fetch_description=False,
        description_format="plain",
        verbose=2,
        **parameters,
    )

    if settings.results_wanted > max_results:
        raise ValueError(f"results_wanted cannot exceed {max_results}.")

    if settings.offset >= max_results:
        raise ValueError(f"offset must be below {max_results}.")

    return settings, float(minimum), batch_size, float(request_delay)


def detect_ad_language(description):
    try:
        return detect(description)
    except LangDetectException:
        return "unknown"


@lru_cache(maxsize=5000)
def english_likelihood_title(title):
    global _title_factory

    if not isinstance(title, str) or not title.strip():
        return None

    if _title_factory is None:
        _title_factory = DetectorFactory()
        _title_factory.seed = 0
        _title_factory.load_profile(PROFILES_DIRECTORY)

    detector = _title_factory.create()

    try:
        detector.append(title)
        detector.get_probabilities()
    except LangDetectException:
        return None

    # langdetect 1.0.9 hides scores below 0.1 in its public list.
    # Read the full computed probabilities to keep the actual English score.
    english_index = detector.langlist.index("en")
    return float(detector.langprob[english_index])


def read_jobs(worksheet):
    rows = worksheet.get(
        "A:H",
        value_render_option="UNFORMATTED_VALUE",
    )

    if rows:
        if list(rows[0][:6]) != COLUMNS[:6]:
            raise ValueError("The first six Jobs headings have changed.")

        for index in (6, 7):
            if len(rows[0]) > index and rows[0][index] not in ("", COLUMNS[index]):
                raise ValueError(
                    f"Column {chr(65 + index)} is already used by another column."
                )

    records = {}

    for row_number, row in enumerate(rows[1:], start=2):
        values = (list(row) + [""] * 8)[:8]
        url = str(values[2] or "").strip()

        if not url:
            continue

        if url in records:
            raise ValueError(f"Duplicate job URL in Jobs: {url}")

        if values[6] not in ("", "done", "pending", SKIPPED_TITLE_STATUS):
            raise ValueError(f"Invalid description_status in row {row_number}.")

        records[url] = (row_number, values)

    return records


def score_and_filter_titles(worksheet, records, minimum):
    updates = []
    skipped = 0

    for row_number, values in records.values():
        score = english_likelihood_title(values[0])
        stored_score = "" if score is None else score
        old_status = values[6]
        status = old_status or "done"

        # Apply the filter before any pending description requests.
        # Existing completed rows keep their download history.
        if status in ("pending", SKIPPED_TITLE_STATUS):
            status = (
                SKIPPED_TITLE_STATUS
                if score is not None and score < minimum
                else "pending"
            )

        if status == SKIPPED_TITLE_STATUS:
            skipped += 1

        if values[7] != stored_score or old_status != status:
            updates.append({
                "range": f"G{row_number}:H{row_number}",
                "values": [[status, stored_score]],
            })

        values[6] = status
        values[7] = stored_score

    if updates:
        worksheet.batch_update(updates, raw=True)

    return skipped


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
            self.log.info(f"search page at offset {start}")
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
                time_tag = card.find("time")
                posted = None
                if time_tag is not None:
                    try:
                        posted = date.fromisoformat(str(time_tag.get("datetime", "")))
                    except ValueError:
                        pass
                batch.append({
                    "title": title_tag.get_text(strip=True) if title_tag else "",
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


def save_batch(worksheet, records, jobs, minimum):
    rows = []
    for job in jobs:
        if job["job_url"] in records:
            continue
        score = english_likelihood_title(job["title"])
        posted = job["date_posted"]
        rows.append([
            job["title"], job["company"], job["job_url"], "",
            (posted - date(1899, 12, 30)).days if posted else "",
            "unknown",
            SKIPPED_TITLE_STATUS if score is not None and score < minimum else "pending",
            "" if score is None else score,
        ])
    if rows:
        # Persist the IDs and scores before making description requests.
        worksheet.append_rows(
            values=rows,
            value_input_option="RAW",
            insert_data_option="INSERT_ROWS",
            table_range="A1:H",
        )
        records = read_jobs(worksheet)
    return records, len(rows)


def process_descriptions(worksheet, records, urls, scraper, attempted):
    updates = []
    completed = 0
    try:
        for url in urls:
            if scraper.monitor.blocked:
                break
            if url in attempted:
                continue
            row, values = records[url]
            if values[6] != "pending":
                continue
            attempted.add(url)
            job_id = url.rstrip("/").rsplit("/", 1)[-1]
            if not job_id.isdigit():
                scraper.log.warning(f"Invalid job URL in row {row}.")
                continue
            scraper.wait_before_request()
            print(f"Downloading description: {job_id}", flush=True)
            try:
                details = scraper._fetch_details(job_id)
                description = details.get("description") or ""
            except Exception as error:
                scraper.log.warning(f"Description failed for {job_id}: {error}")
                continue
            if not description.strip():
                print(f"Description unavailable for {job_id}; left pending.", flush=True)
                continue
            text = description.casefold()
            result = [
                "dutch" in text or "nederlands" in text,
                values[4],
                detect_ad_language(description),
                "done",
            ]
            updates.append({"range": f"D{row}:G{row}", "values": [result]})
            values[3:7] = result
            completed += 1
    finally:
        # Commit this batch before the next search request.
        if updates:
            worksheet.batch_update(updates, raw=True)
    return completed


def main():
    settings, minimum, batch_size, request_delay = load_settings()
    mode = os.environ.get("COLLECTOR_MODE", "all")
    if mode not in ("all", "descriptions"):
        raise ValueError("COLLECTOR_MODE must be all or descriptions.")
    client = gspread.service_account_from_dict(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    worksheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"]).worksheet("Jobs")
    records = read_jobs(worksheet)
    worksheet.update(range_name="A1:H1", values=[COLUMNS], raw=True)
    worksheet.format("E2:E", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}})
    worksheet.format("H2:H", {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}})
    score_and_filter_titles(worksheet, records, minimum)
    old_pending = [url for url, (_, values) in records.items() if values[6] == "pending"]
    old_position = 0
    attempted = set()
    completed = 0
    saved = 0
    monitor = SearchMonitor()
    scraper = Collector(settings, monitor, request_delay)
    scraper.log.setLevel(logging.INFO)
    scraper.log.addHandler(monitor)
    try:
        if mode == "all":
            print(
                f"Searching {settings.location}; lookback: {settings.hours_old} hours; "
                f"batch size: {batch_size}",
                flush=True,
            )
            for jobs in scraper.search_batches(batch_size):
                records, added = save_batch(worksheet, records, jobs, minimum)
                saved += added
                completed += process_descriptions(
                    worksheet, records, [job["job_url"] for job in jobs], scraper, attempted
                )
                print(f"Batch saved: {added} new IDs; {completed} descriptions completed this run.", flush=True)
                if monitor.blocked:
                    break
                # Give the older queue a turn between new batches.
                completed += process_descriptions(
                    worksheet, records,
                    old_pending[old_position:old_position + batch_size], scraper, attempted,
                )
                old_position += batch_size
                if monitor.blocked:
                    break
        else:
            print("Search skipped: retrying saved pending jobs only.", flush=True)

        while old_position < len(old_pending) and not monitor.blocked:
            completed += process_descriptions(
                worksheet, records,
                old_pending[old_position:old_position + batch_size], scraper, attempted,
            )
            old_position += batch_size

        pending = sum(values[6] == "pending" for _, values in records.values())
        skipped = sum(values[6] == SKIPPED_TITLE_STATUS for _, values in records.values())
        print(
            f"New IDs saved: {saved}; descriptions attempted: {len(attempted)}; "
            f"completed: {completed}; pending: {pending}; skipped by title: {skipped}",
            flush=True,
        )
        issues = list(monitor.messages)
        if pending:
            issues.append(f"{pending} descriptions remain pending for a later run.")
        if monitor.blocked:
            issues.append("LinkedIn returned 403 or 429; all further LinkedIn requests stopped.")
        for issue in dict.fromkeys(issues):
            print(f"ATTENTION: {issue}", flush=True)
        return 1 if issues else 0
    finally:
        scraper.log.removeHandler(monitor)
        scraper.session.close()


if __name__ == "__main__":
    raise SystemExit(main())

