import json
import logging
import os

import gspread
import pandas as pd
from jobspy.linkedin import LinkedIn
from jobspy.model import ScraperInput
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException


logging.basicConfig(level=logging.INFO)
DetectorFactory.seed = 0


def detect_ad_language(description):
    if not isinstance(description, str) or not description.strip():
        return "unknown"

    try:
        return detect(description)
    except LangDetectException:
        return "unknown"


# Extend JobSpy's LinkedIn scraper
class CachedLinkedIn(LinkedIn):
    def __init__(self, saved_urls):
        super().__init__()
        self.saved_urls = saved_urls
        self.description_downloads = 0

    def _fetch_details(self, job_id):
        job_url = f"{self.base_url}/jobs/view/{job_id}"

        # Already saved: skip the description request
        if job_url in self.saved_urls:
            return {}

        # New job: use JobSpy's original downloader
        self.description_downloads += 1
        return super()._fetch_details(job_id)


# Connect to Google Sheets
credentials_info = json.loads(
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
)

client = gspread.service_account_from_dict(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)

spreadsheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])
worksheet = spreadsheet.worksheet("Jobs")


# Write column headings
columns = [
    "title",
    "company",
    "job_url",
    "mentions_dutch",
    "date_posted",
    "ad_language",
]

worksheet.update(
    range_name="A1:F1",
    values=[columns],
    raw=True,
)


# Read saved URLs from column C
existing_urls = {
    url.strip()
    for url in worksheet.col_values(3)[1:]
    if isinstance(url, str) and url.strip()
}

print(f"Already saved: {len(existing_urls)} distinct job URLs")


# Create our scraper with the saved URLs
scraper = CachedLinkedIn(existing_urls)

search_settings = ScraperInput(
    site_name=["linkedin"],
    search_term="data analyst",
    location="Netherlands",
    distance=50,
    results_wanted=5,
    fetch_description=True,
    description_format="plain",
    verbose=2,
)


# Search LinkedIn
print("Searching LinkedIn...", flush=True)

job_posts = scraper.scrape(search_settings)

# Convert JobSpy's job objects into a pandas table
jobs = pd.DataFrame([
    job.model_dump()
    for job in job_posts
])

print(f"Returned {len(jobs)} jobs")
print(
    "Search errors:",
    {"linkedin": scraper.error} if scraper.error else {},
)
print(
    f"Description downloads attempted: {scraper.description_downloads}"
)


# Keep only new URLs
if not jobs.empty:
    jobs["job_url"] = jobs["job_url"].fillna("").str.strip()

    has_url = jobs["job_url"] != ""
    is_new = ~jobs["job_url"].isin(existing_urls)

    jobs = (
        jobs[has_url & is_new]
        .drop_duplicates(subset=["job_url"])
        .copy()
    )


# Leave failed descriptions unsaved so a later run can retry them
if not jobs.empty:
    has_description = (
        jobs["description"].fillna("").str.strip() != ""
    )

    print(
        f"New jobs without a description: {(~has_description).sum()}"
    )

    jobs = jobs[has_description].copy()


# Prepare and append new jobs
if not jobs.empty:
    jobs["mentions_dutch"] = (
        jobs["description"]
        .fillna("")
        .str.contains(
            "dutch|nederlands",
            case=False,
            na=False,
        )
    )

    jobs["ad_language"] = jobs["description"].apply(
        detect_ad_language
    )

    output = jobs.reindex(columns=columns).copy()
    print(output.to_string(index=False))

    publication_dates = pd.to_datetime(
        output["date_posted"],
        errors="coerce",
        utc=True,
    )

    output["date_posted"] = (
        publication_dates
        - pd.Timestamp("1899-12-30", tz="UTC")
    ).dt.days

    rows = output.fillna("").values.tolist()

    worksheet.append_rows(
        values=rows,
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
        table_range="A1:F",
    )

    worksheet.format(
        "E2:E",
        {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}},
    )

    print(f"Added {len(rows)} new jobs.", flush=True)

else:
    print(
        "No new jobs with a readable description to add.",
        flush=True,
    )

