# Catch-up update

1. Replace `main.py` at the root of your GitHub repository.
2. Add `catchup.yaml` beside `main.py`.
3. Replace the existing `.github/workflows/test.yaml` with the included workflow.
4. Keep your current `search.yaml`, `config_engine.py`, and filter YAML files.
5. Commit the files, then use Actions > LinkedIn Job Collector > Run workflow on main, mode all.

The collector creates a `Collector_State` tab in the same Google spreadsheet.
Checkpoints are independent for each query and location. No extra credentials are needed.

The first run for a query/location searches the preceding 24 hours. Existing job URLs are deduplicated.
Later runs use the larger of `search.yaml` hours_old and the elapsed time since the last completed
search plus a one-hour overlap. These values can be changed in `catchup.yaml`.
A failed initial attempt retains its original window, even across new GitHub VMs.
The lower time boundary stays fixed while a long search runs.

The checkpoint advances only after all discovered IDs for an observed completed search have been saved.
HTTP 403/429, unexpected HTML, unreadable job IDs, result caps, and pagination ceilings leave it unchanged.
The standard empty LinkedIn results page and a short results page are treated as endpoint completion.
LinkedIn can still omit or reorder results; this cannot guarantee a complete inventory of vacancies.

Description and LLM progress are separate from discovery progress. If the LLM fails, further paid calls
pause for that execution while metadata and descriptions continue to be saved. Pending work retries
on the next run; mode descriptions retries saved jobs without searching again.
Google Sheets 429 responses trigger bounded retries, and writes are batched and paced.

Logs use UTC timestamps. `record_id` combines the LinkedIn posting ID and first-discovery timestamp,
and remains fixed across retries. Existing rows are upgraded in place using their first_seen_at values.

The workflow runs every three hours at minute 17 UTC. GitHub may still delay or drop scheduled starts;
the persistent checkpoint provides the catch-up window when the next execution starts.

Validated locally with 31 simulated tests, including delayed starts, incomplete searches,
independent locations, API quotas, state-write failures, cached retries and schema migration.
No live LinkedIn, Sheets or LLM requests were made by those tests.
