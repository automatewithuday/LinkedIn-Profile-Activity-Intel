# AGENTS.md

Qualifies LinkedIn outreach lists: checks whether each profile is publicly active, and when, so only people who
actually use LinkedIn go into a campaign. Evidence comes from three
HarvestAPI actors on Apify (posts, comments, reactions); code computes the dates and counts;
TypeSafe (Jev) makes the status / level / confidence / outreach-ready judgments.
One module: `activity_intel.py`. No LLM text generation anywhere.

## Run
```sh
cp .env.example .env            # user fills in APIFY_TOKEN and TYPESAFE_API_KEY
uv sync
uv run activity_intel.py leads.csv --dry-run              # ALWAYS first: profile count, Apify estimate AND hard cap
uv run --env-file .env activity_intel.py leads.csv        # writes out/<run>.jsonl + out/<run>.csv
uv run --env-file .env activity_intel.py leads.csv --from-raw out/raw/<run>   # re-judge, no Apify spend
uv run pytest -q
```
Input: CSV with a `linkedin_url` column, or a text file with one profile URL per line.
Options: `--limit N`, `--config path`, `--out dir`.

## Rules for agents
- `config.toml` is user-owned (fetch caps, thresholds, TypeSafe question wording).
  Propose changes; never edit its values unprompted. The 7/30/90-day windows are fixed in code (`WINDOWS`):
  output field names and question text depend on them.
- Run `--dry-run` and tell the user BOTH numbers before any paid run: the estimate (expected charge) and the hard cap
  (sum of per-run `max_total_charge_usd` = estimate + 25% + $0.05 per actor run; $0.20 for a single profile).
  Do not remove the cap or loop around a spend/billing error.
- Secrets live in `.env` only. Never print, echo, commit, or paste them.
- Python via `uv` only. Quote paths (they may contain spaces).
- Report results as they are: failed runs, unattributed items, `rejected_evidence_count > 0`, and
  `status_matches_date_rule = false` rows are findings, not noise to hide. Rows with `data_quality_warning = true`
  or `status_matches_date_rule = false` should be held back for human review before any campaign export.

## Output (one row per input profile)
| field | source | meaning |
|---|---|---|
| `linkedin_outreach_ready`, `outreach_ready_probability` | TypeSafe | THE verdict: is this profile worth reaching out to on LinkedIn now. Probability ≥ `judge.outreach_ready_threshold`; sort by probability to prioritise |
| `outreach_reason` | code | plain-English reasoning assembled from the facts + Jev's answers (TypeSafe cannot write text; no LLM involved) |
| `days_since_last_activity`, `last_activity_type`, `most_recent_activity_url` | code | newest evidence of any kind (days, 1 decimal; rounded UP for reactions since it is an upper bound); the URL for a `reaction` is the post reacted to. TypeSafe receives the unrounded ages |
| `activity_status` | TypeSafe | `active` (≤30d) / `stale` (31–90d) / `inactive` (>90d); `no_activity_observed` set by code when no evidence; null if fetch failed |
| `activity_level` | TypeSafe | `minimal` / `low` / `moderate` / `high` (`none` when no evidence) |
| `activity_confidence` | TypeSafe | `low` / `medium` / `high` — how well the evidence supports the timing; code caps it at `medium` when no exactly-dated evidence exists |
| `typesafe_status_confidence` | TypeSafe | 0–1 concentration of the status choice |
| `status_matches_date_rule` | code | false = Jev disagreed with the plain 30/90-day rule; review these |
| `active_7d/30d/90d`, `recent_activity_evidence_at` | code | from the newest evidence of any kind; windows are tested in ms (30.9 days is not "within 30") |
| `most_recent_exact_activity_at` | code | newest post/repost/comment — the newest exactly-dated action; null for reaction-only profiles |
| `samples_at_cap` | code | sources that returned as many items as the cap allows (`posts,comments,reactions`): newer activity may exist outside the sample |
| `rejected_evidence_count` | code | items dropped for a missing/malformed/future timestamp; sets `data_quality_warning` |
| `most_recent_observed_authored_post_at` / `_comment_at`, `most_recent_post_url` / `_comment_url` | code | null = not observed in the sample, not "never" |
| `post/repost/comment/reaction_evidence_count`, `recent_post/repost/comment_evidence_count_30d`, `recent_reaction_evidence_count_7d/30d/90d` | code | counts within the fetched sample (caps in config), not lifetime totals |
| `success`, `status_code`, `data_quality_warning`, `error` | code | 502 when a source or TypeSafe failed for THIS profile (`success` false only when nothing came back or judging failed); 500 for an unexpected per-profile exception; `error` accumulates every message. A failed fetch leaves `activity_status` null, never `no_activity_observed` |

## Reaction dating rule
LinkedIn exposes no timestamp for a reaction. A reaction cannot be older than the post it is on, so
the post's date is used as a lower bound: a reaction on a 5-day-old post proves activity within 7 days.
A reaction on an old post proves nothing recent and is never counted as recent.

## Verified against live output (2026-09-22)
- Every item echoes its input: posts `query.targetUrl`, comments/reactions `query.profile`. `attribute()` matches on it first.
- Reaction items have `createdAt`/`createdAtTimestamp`, but they equal `post.postedAt` to the millisecond — not a real
  reaction time. Keep the lower-bound rule; do not "upgrade" to `createdAt`.
- Comments are NOT returned strictly newest-first; code sorts, but with a small `max_comments` the newest comment can be missed.

- Multi-profile run (10 profiles, 185 items): `query` attributed every item; 0 unmatched.
- Reposts carry `repostedBy` + `repostedAt`, and `postedAt` equals `repostedAt` (the person's own action time). `author` is the
  original poster. Code counts them as `repost` evidence, never as authored posts.

- Apify `call()` returns FAILED / TIMED-OUT / ABORTED runs as normal objects with an empty or partial dataset; `fetch()` treats
  anything but `SUCCEEDED` as a failure for that run's profiles. Run ids, statuses and charge counters are in
  `out/raw/<run>/manifest.json` with the queried profiles, timestamp and config. Charge fields (`charged_events`, `usage_usd`)
  are observed at run end and lag, often showing 0: every entry carries `charges_final: false` and `charges_observed_at`,
  so never read them as a final bill; the Apify Console has it. Charge/log lookups are best-effort (`charges_error` /
  `log_error` when they fail) and never discard scraped data. But the log is the only way to tell an empty profile from a
  skipped one, so when it is unavailable every profile that returned zero items for that source is marked
  `collection unverified` (502 for that source): with no other evidence its status stays null, never `no_activity_observed`.
- A `SUCCEEDED` HarvestAPI run can still skip profiles: live, the posts actor logged `Error scraping item#N {...}: "Too many
  queued requests (code_22)"` for 3-4 of 10 profiles and returned 0 posts for them. `fetch()` scans the run log for that
  line, drops the affected profiles' partial items, retries them once in a separate run, and on a second failure (or a
  retry exception) marks only them 502 for that source; the first run's results and run id are kept. This is a log-format
  heuristic; if unmatched-but-empty profiles appear, check the run log.
- `attribute()` treats the `query` echo as authoritative: an item whose query owner is not in the current input is unmatched,
  never reassigned via `author`/`repostedBy`. With `--from-raw` on a subset of the cached profiles, unmatched counts are expected.

If stderr reports unmatched items on a LIVE run, inspect `out/raw/<run>/*.json`.

## Tests
`uv run pytest -q` — 21 offline tests (no network): evidence rules, window edges, replay ownership, failed runs, malformed
timestamps, per-chunk error isolation, TypeSafe failure, cost/cap agreement, confidence ceiling. CI runs them on every push.
