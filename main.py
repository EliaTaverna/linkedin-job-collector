import json
import os

import gspread
import pandas as pd
from jobspy import scrape_jobs
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException


# Make language detection consistent between runs
DetectorFactory.seed = 0


def detect_ad_language(description):
    if not isinstance(description, str) or not description.strip():
        return "unknown"

    try:
        return detect(description)
    except LangDetectException:
        return "unknown"


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


# Write only the column headings
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


# Read URLs already saved in column C, skipping the header
existing_urls = {
    url.strip()
    for url in worksheet.col_values(3)[1:]
    if isinstance(url, str) and url.strip()
}

print(f"Already saved: {len(existing_urls)} distinct job URLs")


# Search LinkedIn
print("Searching LinkedIn...", flush=True)

jobs = scrape_jobs(
    site_name=["linkedin"],
    search_term="data analyst",
    location="Netherlands",
    results_wanted=5,
    fetch_description=True,
    description_format="plain",
    verbose=2,
)

print(f"Returned {len(jobs)} jobs")
print("Search errors:", jobs.attrs.get("errors", {}))


# Keep jobs whose URLs are not already in the sheet
if not jobs.empty:
    jobs["job_url"] = jobs["job_url"].fillna("").str.strip()

    has_url = jobs["job_url"] != ""
    is_new = ~jobs["job_url"].isin(existing_urls)

    jobs = (
        jobs[has_url & is_new]
        .drop_duplicates(subset=["job_url"])
        .copy()
    )


# Prepare and append the new rows
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

    # Convert publication dates into Google Sheets date numbers
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

    # Append new rows beneath the existing jobs
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
    print("No new jobs to add.", flush=True)
