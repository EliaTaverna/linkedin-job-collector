import json
import os

import gspread
import pandas as pd
from jobspy import scrape_jobs
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException


# Make language detection consistent between runs
DetectorFactory.seed = 0


# Detect the language of an advert
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


# Prepare the table
columns = [
    "title",
    "company",
    "job_url",
    "mentions_dutch",
    "date_posted",
    "ad_language",
]

rows = [columns]

if not jobs.empty:
    # Check whether the description mentions Dutch
    jobs["mentions_dutch"] = (
        jobs["description"]
        .fillna("")
        .str.contains(
            "dutch|nederlands",
            case=False,
            na=False,
        )
    )

    # Run our language-detection function on each description
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

    rows += output.fillna("").values.tolist()


# Replace columns A–F with the latest results
worksheet.batch_clear(["A:F"])

worksheet.update(
    range_name="A1",
    values=rows,
    raw=True,
)

# Display publication dates as day/month/year
worksheet.format(
    "E2:E",
    {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}},
)

print(f"Saved {len(jobs)} jobs to Google Sheets.", flush=True)
