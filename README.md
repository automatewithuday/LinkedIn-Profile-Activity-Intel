# LinkedIn Profile Activity Intel

Is this LinkedIn profile active, and when was it last active? Bring your own keys.

- Evidence: HarvestAPI actors on Apify — [posts](https://apify.com/harvestapi/linkedin-profile-posts),
  [comments](https://apify.com/harvestapi/linkedin-profile-comments),
  [reactions](https://apify.com/harvestapi/linkedin-profile-reactions). No LinkedIn cookies or account.
- Judgments: [TypeSafe](https://docs.typesafe.ai) (Jev) — status, level, confidence, outreach-ready.

```sh
cp .env.example .env     # add APIFY_TOKEN and TYPESAFE_API_KEY
uv sync
uv run activity_intel.py example_leads.csv --dry-run
uv run --env-file .env activity_intel.py example_leads.csv
```

Cost at default caps (5 posts / 5 comments / 10 reactions): at most ~$0.04 per profile on Apify,
TypeSafe well under $0.0001. Change caps, windows, thresholds and question wording in `config.toml`.

Using a coding agent (Claude Code, Codex, Cursor, …)? It reads `AGENTS.md` — output schema and rules are there.
