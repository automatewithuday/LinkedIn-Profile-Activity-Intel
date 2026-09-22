# AGENTS.md

Checks whether LinkedIn profiles are publicly active, and when. Evidence comes from three
HarvestAPI actors on Apify (posts, comments, reactions); code computes the dates and counts;
TypeSafe (Jev) makes the status / level / confidence / outreach-ready judgments.
One module: `activity_intel.py`. No LLM text generation anywhere.

## Run
```sh
cp .env.example .env            # user fills in APIFY_TOKEN and TYPESAFE_API_KEY
uv sync
uv run activity_intel.py leads.csv --dry-run              # ALWAYS first: profile count + max Apify cost
uv run --env-file .env activity_intel.py leads.csv        # writes out/<run>.jsonl + out/<run>.csv
uv run --env-file .env activity_intel.py leads.csv --from-raw out/raw/<run>   # re-judge, no Apify spend
uv run pytest -q
```
Input: CSV with a `linkedin_url` column, or a text file with one profile URL per line.
Options: `--limit N`, `--config path`, `--out dir`.

## Rules for agents
- `config.toml` is user-owned (fetch caps, windows, thresholds, TypeSafe question wording).
  Propose changes; never edit its values unprompted.
- Run `--dry-run` and tell the user the cost before any paid run. Each Apify run carries a hard
  `max_total_charge_usd` cap — do not remove it or loop around a spend/billing error.
- Secrets live in `.env` only. Never print, echo, commit, or paste them.
- Python via `uv` only. Quote paths (they may contain spaces).
- Report results as they are: failed actors, unattributed items, and `status_matches_date_rule = false`
  rows are findings, not noise to hide.

## Output (one row per input profile)
| field | source | meaning |
|---|---|---|
| `linkedin_outreach_ready`, `outreach_ready_probability` | TypeSafe | THE verdict: is this profile worth reaching out to on LinkedIn now. Probability ≥ `judge.outreach_ready_threshold`; sort by probability to prioritise |
| `outreach_reason` | code | plain-English reasoning assembled from the facts + Jev's answers (TypeSafe cannot write text; no LLM involved) |
| `days_since_last_activity`, `last_activity_type` | code | newest evidence of any kind; for `reaction` the age is an upper bound |
| `activity_status` | TypeSafe | `active` (≤30d) / `stale` (31–90d) / `inactive` (>90d); `no_activity_observed` set by code when no evidence; null if fetch failed |
| `activity_level` | TypeSafe | `minimal` / `low` / `moderate` / `high` (`none` when no evidence) |
| `activity_confidence` | TypeSafe | `low` / `medium` / `high` — how well the evidence supports the timing |
| `typesafe_status_confidence` | TypeSafe | 0–1 concentration of the status choice |
| `status_matches_date_rule` | code | false = Jev disagreed with the plain 30/90-day rule; review these |
| `active_7d/30d/90d`, `recent_activity_evidence_at` | code | from the newest evidence of any kind |
| `most_recent_observed_authored_post_at` / `_comment_at`, `most_recent_post_url` / `_comment_url` | code | null = not observed in the sample, not "never" |
| `post/repost/comment/reaction_evidence_count`, `recent_post/repost/comment_evidence_count_30d`, `recent_reaction_evidence_count_7d/30d/90d` | code | counts within the fetched sample (caps in config), not lifetime totals |
| `success`, `status_code`, `data_quality_warning`, `error` | code | 502 + warning when an actor or TypeSafe call failed |

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

If stderr ever reports items that "matched no input profile", inspect `out/raw/<run>/*.json`.
