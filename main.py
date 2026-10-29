from jobspy import scrape_jobs

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

if not jobs.empty:
    jobs["description_characters"] = (
        jobs["description"].fillna("").str.len()
    )

    print(
        jobs[["title", "description_characters"]]
        .to_string(index=False)
    )
