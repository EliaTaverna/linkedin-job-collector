from jobspy import scrape_jobs

print("Searching LinkedIn...", flush=True)

jobs = scrape_jobs(
    site_name=["linkedin"],
    search_term="data analyst",
    location="Netherlands",
    results_wanted=5,
    fetch_description=False,
    verbose=2,
)

print(f"Returned {len(jobs)} jobs")
print("Search errors:", jobs.attrs.get("errors", {}))

if not jobs.empty:
    print(jobs[["title", "company", "job_url"]].to_string(index=False))
