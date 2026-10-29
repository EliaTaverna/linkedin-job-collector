import json
import os

import gspread
from jobspy import scrape_jobs


# Connessione a Google Sheets
credentials_info = json.loads(
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
)

client = gspread.service_account_from_dict(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)

spreadsheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])
worksheet = spreadsheet.worksheet("Jobs")

worksheet.update_acell("A1", "Connected from GitHub!")
print("Google Sheets connection successful.", flush=True)


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
