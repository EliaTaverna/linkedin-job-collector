import json
import os

import gspread
from jobspy import scrape_jobs


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
columns = ["title", "company", "job_url", "mentions_dutch"]
rows = [columns]

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

    output = jobs[columns].fillna("")
    rows += output.values.tolist()

    print(output.to_string(index=False))


# Replace columns A–D with the latest results
worksheet.batch_clear(["A:D"])
worksheet.update(
    range_name="A1",
    values=rows,
    raw=True,
)

print(f"Saved {len(jobs)} jobs to Google Sheets.", flush=True)

# Ricerca degli annunci su LinkedIn
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


# Controllo delle parole "Dutch" e "Nederlands"
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

    print(
        jobs[["title", "company", "mentions_dutch", "job_url"]]
        .to_string(index=False)
    )
