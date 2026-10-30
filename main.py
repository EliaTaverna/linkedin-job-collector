import json
import logging
import os
from datetime import date
from pathlib import Path

import gspread
import yaml
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.constant import jobs_per_page, max_results
from jobspy.model import ScraperInput
from langdetect import DetectorFactory, detect
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
]


def load_settings():
    path = Path(__file__).resolve().with_name("search.yaml")

    with path.open(encoding="utf-8") as config_file:
        parameters = yaml.safe_load(config_file)

    if not isinstance(parameters, dict):
        raise ValueError("search.yaml must contain key: value settings.")

    allowed = {
        "search_term",
        "location",
        "results_wanted",
        "distance",
        "hours_old",
        "offset",
    }

    unknown = set(parameters) - allowed

    if unknown:
        raise ValueError(f"Unknown search.yaml settings: {unknown}")

    for name in (
        "search_term",
        "location",
        "results_wanted",
        "hours_old",
    ):
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
        raise ValueError(
            f"results_wanted cannot exceed {max_results}."
        )

    if settings.offset >= max_results:
        raise ValueError(
            f"offset must be below {max_results}."
        )

    return settings


def detect_ad_language(description):
    try:
        return detect(description)
    except LangDetectException:
        return "unknown"


def read_jobs(worksheet):
    rows = worksheet.get(
        "A:G",
        value_render_option="UNFORMATTED_VALUE",
    )

    if rows:
        if list(rows[0][:6]) != COLUMNS[:6]:
            raise ValueError(
                "The first six Jobs headings have changed."
            )

        if len(rows[0]) > 6 and rows[0][6] not in (
            "",
            COLUMNS[6],
        ):
            raise ValueError(
                "Column G is already used by another column."
            )

    records = {}

    for row_number, row in enumerate(rows[1:], start=2):
        values = (list(row) + [""] * 7)[:7]
        url = str(values[2] or "").strip()

        if not url:
            continue

        if url in records:
            raise ValueError(
                f"Duplicate job URL in Jobs: {url}"
            )

        if values[6] not in ("", "done", "pending"):
            raise ValueError(
                f"Invalid description_status in row {row_number}."
            )

        records[url] = (row_number, values)

    return records


class SearchMonitor(logging.Handler):
    def __init__(self):
        super().__init__()
        self.pages = 0
        self.messages = []
        self.blocked = False

    def emit(self, record):
        message = record.getMessage()

        if message.startswith("search page:"):
            self.pages += 1

        if record.levelno >= logging.WARNING:
            self.messages.append(message)

        if any(
            f"status code {code}" in message
            for code in (403, 429)
        ):
            self.blocked = True


class Collector(LinkedIn):
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.discovered_ids = []
        self.last_page_with_ids = 0

    def _process_job(self, job_card, job_id):
        # Remember the ID even if parsing other fields fails
        self.discovered_ids.append(job_id)
        self.last_page_with_ids = self.monitor.pages

        return super()._process_job(job_card, job_id)


def main():
    settings = load_settings()

    # Connect to Google Sheets
    credentials = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )

    client = gspread.service_account_from_dict(
        credentials,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets"
        ],
    )

    worksheet = client.open_by_key(
        os.environ["GOOGLE_SHEET_ID"]
    ).worksheet("Jobs")

    records = read_jobs(worksheet)

    worksheet.update(
        range_name="A1:G1",
        values=[COLUMNS],
        raw=True,
    )

    # Rows saved by the previous version are already processed
    legacy_updates = [
        {
            "range": f"G{row}",
            "values": [["done"]],
        }
        for row, values in records.values()
        if values[6] == ""
    ]

    if legacy_updates:
        worksheet.batch_update(
            legacy_updates,
            raw=True,
        )

    print(
        f"Searching {settings.location}; "
        f"lookback: {settings.hours_old} hours",
        flush=True,
    )

    monitor = SearchMonitor()
    scraper = Collector(monitor)

    scraper.log.setLevel(logging.INFO)
    scraper.log.addHandler(monitor)

    try:
        # PHASE 1: discover IDs without downloading descriptions
        job_posts = scraper.scrape(settings)

        jobs_by_url = {
            job.job_url: job
            for job in job_posts
        }

        issues = []

        if scraper.error:
            issues.append(
                f"Search interrupted: {scraper.error}"
            )

        if monitor.messages:
            issues.append(
                "Search produced warnings; inspect the log."
            )

        if len(job_posts) >= settings.results_wanted:
            issues.append(
                "Configured result limit reached; "
                "more jobs may exist."
            )

        if (
            settings.offset
            + monitor.pages * jobs_per_page
            >= max_results
        ):
            issues.append(
                "JobSpy pagination ceiling reached."
            )

        if settings.offset:
            issues.append(
                "A nonzero offset skipped "
                "the beginning of the search."
            )

        if (
            monitor.pages > 1
            and scraper.last_page_with_ids < monitor.pages
            and not scraper.error
            and not monitor.messages
        ):
            issues.append(
                "Pagination stopped without new IDs; "
                "coverage is uncertain."
            )

        # Save new IDs before attempting their descriptions
        new_rows = []

        for job_id in scraper.discovered_ids:
            url = (
                f"{scraper.base_url}/jobs/view/{job_id}"
            )

            if url in records:
                continue

            job = jobs_by_url.get(url)
            posted = job.date_posted if job else None

            serial_date = (
                (posted - date(1899, 12, 30)).days
                if posted is not None
                else ""
            )

            new_rows.append([
                job.title if job else "",
                (job.company or "") if job else "",
                url,
                "",
                serial_date,
                "unknown",
                "pending",
            ])

        if new_rows:
            worksheet.append_rows(
                values=new_rows,
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
                table_range="A1:G",
            )

        worksheet.format(
            "E2:E",
            {
                "numberFormat": {
                    "type": "DATE",
                    "pattern": "dd/mm/yyyy",
                }
            },
        )

        print(
            f"Search pages: {monitor.pages}; "
            f"IDs discovered: {len(scraper.discovered_ids)}; "
            f"new IDs saved: {len(new_rows)}",
            flush=True,
        )

        # Read actual row positions after appending
        records = read_jobs(worksheet)

        pending = [
            (url, row, values)
            for url, (row, values) in records.items()
            if values[6] == "pending"
        ]

        # PHASE 2: download pending descriptions
        # This also retries older jobs from previous failed runs
        updates = []
        completed = 0
        attempted = 0

        try:
            for url, row, values in pending:
                if monitor.blocked:
                    break

                job_id = (
                    url.rstrip("/").rsplit("/", 1)[-1]
                )

                if not job_id.isdigit():
                    issues.append(
                        f"Invalid job URL in row {row}."
                    )
                    continue

                print(
                    f"Downloading description: {url}",
                    flush=True,
                )

                attempted += 1

                try:
                    details = scraper._fetch_details(
                        job_id
                    )

                    description = (
                        details.get("description") or ""
                    )

                except Exception as error:
                    print(
                        f"Description failed for "
                        f"{job_id}: {error}"
                    )
                    continue

                if not description.strip():
                    print(
                        f"Description unavailable for "
                        f"{job_id}; left pending."
                    )
                    continue

                text = description.casefold()

                mentions_dutch = (
                    "dutch" in text
                    or "nederlands" in text
                )

                updates.append({
                    "range": f"D{row}:G{row}",
                    "values": [[
                        mentions_dutch,
                        values[4],
                        detect_ad_language(description),
                        "done",
                    ]],
                })

                completed += 1

                # Save progress in batches
                if len(updates) >= 20:
                    worksheet.batch_update(
                        updates,
                        raw=True,
                    )
                    updates.clear()

        finally:
            if updates:
                worksheet.batch_update(
                    updates,
                    raw=True,
                )

        remaining = len(pending) - completed

        print(
            f"Descriptions attempted: {attempted}; "
            f"completed: {completed}; "
            f"still pending: {remaining}",
            flush=True,
        )

        if remaining:
            issues.append(
                f"{remaining} descriptions remain "
                "pending for retry."
            )

        if monitor.blocked:
            issues.append(
                "LinkedIn returned 403 or 429; "
                "further downloads stopped."
            )

        if issues:
            for issue in dict.fromkeys(issues):
                print(
                    f"ATTENTION: {issue}",
                    flush=True,
                )

            print(
                "Run needs attention. Saved rows "
                "and pending IDs are retained."
            )

            return 1

        print(
            "Finished with no detected errors "
            "or result limits.",
            flush=True,
        )

        return 0

    finally:
        scraper.log.removeHandler(monitor)


if __name__ == "__main__":
    raise SystemExit(main())
