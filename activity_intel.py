"""LinkedIn profile activity check: HarvestAPI (Apify) evidence -> code-computed facts -> TypeSafe judgments.

uv run --env-file .env activity_intel.py leads.csv [--dry-run] [--limit N] [--from-raw out/raw/<run>]
"""
import argparse
import csv
import json
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

ACTORS = {
    "posts": "harvestapi/linkedin-profile-posts",
    "comments": "harvestapi/linkedin-profile-comments",
    "reactions": "harvestapi/linkedin-profile-reactions",
}
FIELDS = [
    "success", "status_code", "linkedin_url", "username",
    "linkedin_outreach_ready", "outreach_ready_probability", "outreach_reason",
    "activity_status", "activity_level", "activity_confidence",
    "days_since_last_activity", "last_activity_type",
    "data_quality_warning", "active_7d", "active_30d", "active_90d",
    "recent_activity_evidence_at", "most_recent_observed_authored_post_at",
    "most_recent_observed_authored_comment_at",
    "post_evidence_count", "repost_evidence_count", "comment_evidence_count", "reaction_evidence_count",
    "recent_post_evidence_count_30d", "recent_repost_evidence_count_30d", "recent_comment_evidence_count_30d",
    "recent_reaction_evidence_count_7d", "recent_reaction_evidence_count_30d",
    "recent_reaction_evidence_count_90d",
    "most_recent_post_url", "most_recent_comment_url",
    "typesafe_status_confidence", "status_matches_date_rule",
    "error", "analyzed_at",
]
_IN = re.compile(r"linkedin\.com/in/([^/?#\s\"']+)", re.I)


def username(url):
    m = _IN.search(url or "")
    return unquote(m.group(1)).lower() if m else None


def load_urls(path):
    """CSV with a linkedin_url column, or any text file with one URL per line. Deduped, order kept."""
    text = Path(path).read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    if rows and "linkedin_url" in rows[0]:
        lines = [r["linkedin_url"] for r in rows]
    else:
        lines = text.splitlines()
    users = dict.fromkeys(u for u in map(username, lines) if u)
    return [f"https://www.linkedin.com/in/{u}" for u in users]


# ---------- evidence (pure) ----------

def attribute(kind, items, usernames):
    """Group one actor's items by the queried profile. Returns ({username: [items]}, unattributed_count)."""
    out = {u: [] for u in usernames}
    missed = 0
    for it in items:
        # `query` (the actor's echo of its input) is what matches on live data; the rest are fallbacks.
        cands = [
            username(json.dumps(it.get("query"))) if it.get("query") else None,
            username((it.get("actor") or {}).get("linkedinUrl")) if kind != "posts" else None,
            ((it.get("repostedBy") or {}).get("publicIdentifier") or "").lower() or None,
            ((it.get("author") or {}).get("publicIdentifier") or "").lower() or None if kind == "posts" else None,
        ]
        owner = next((c for c in cands if c in out), None)
        if owner:
            out[owner].append(it)
        else:
            missed += 1
    return out, missed


def _ts(kind, it):
    """Evidence timestamp in ms. For reactions this is the post's date: a lower bound on when the reaction happened.
    (Reaction items do carry createdAt, but live data shows it is a millisecond-exact copy of post.postedAt.)"""
    if kind == "comments":
        t = it.get("createdAtTimestamp")
        if t is None and it.get("createdAt"):
            t = datetime.fromisoformat(str(it["createdAt"]).replace("Z", "+00:00")).timestamp() * 1000
        return t
    src = it.get("post") or {} if kind == "reactions" else it
    return (src.get("postedAt") or {}).get("timestamp")


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else None


def build_evidence(posts, comments, reactions, now, windows):
    """-> (facts dict with LinkedPulse-style fields, evidence list for TypeSafe)."""
    now_ms = now.timestamp() * 1000
    # a repost is the person's own action (live data: postedAt == repostedAt) but not an authored post
    posts, reposts = [p for p in posts if not p.get("repostedBy")], [p for p in posts if p.get("repostedBy")]
    dated = {k: sorted(((_ts(k, it), it) for it in items if _ts(k, it)), key=lambda p: -p[0])
             for k, items in (("posts", posts), ("reposts", reposts), ("comments", comments), ("reactions", reactions))}
    age = lambda ms: max(0, int((now_ms - ms) // 86_400_000))
    within = lambda ms, days: ms is not None and age(ms) <= days
    latest = {k: (v[0][0] if v else None) for k, v in dated.items()}
    newest = max((t for t in latest.values() if t), default=None)
    r7, r30, r90 = windows["recent"], windows["active"], windows["stale"]

    facts = {
        "days_since_last_activity": age(newest) if newest else None,  # for reactions: at most this many days
        "last_activity_type": next((k[:-1] for k in ("posts", "reposts", "comments", "reactions") if newest and latest[k] == newest), None),
        "recent_post_evidence_count_30d": sum(within(t, r30) for t, _ in dated["posts"]),
        "recent_repost_evidence_count_30d": sum(within(t, r30) for t, _ in dated["reposts"]),
        "recent_comment_evidence_count_30d": sum(within(t, r30) for t, _ in dated["comments"]),
        "active_7d": within(newest, r7),
        "active_30d": within(newest, r30),
        "active_90d": within(newest, r90),
        "recent_activity_evidence_at": _iso(newest),
        "most_recent_observed_authored_post_at": _iso(latest["posts"]),
        "most_recent_observed_authored_comment_at": _iso(latest["comments"]),
        "post_evidence_count": len(dated["posts"]),
        "repost_evidence_count": len(dated["reposts"]),
        "comment_evidence_count": len(dated["comments"]),
        "reaction_evidence_count": len(dated["reactions"]),
        "most_recent_post_url": dated["posts"][0][1].get("linkedinUrl") if dated["posts"] else None,
        "most_recent_comment_url": dated["comments"][0][1].get("linkedinUrl") if dated["comments"] else None,
    }
    for d, name in ((r7, "7d"), (r30, "30d"), (r90, "90d")):
        facts[f"recent_reaction_evidence_count_{name}"] = sum(within(t, d) for t, _ in dated["reactions"])
    facts["date_rule_status"] = ("no_activity_observed" if newest is None else "active" if facts["active_30d"]
                                 else "stale" if facts["active_90d"] else "inactive")

    evidence = [{"type": "post", "age_days": age(t)} for t, _ in dated["posts"]]
    evidence += [{"type": "repost", "age_days": age(t)} for t, _ in dated["reposts"]]
    evidence += [{"type": "comment", "age_days": age(t)} for t, _ in dated["comments"]]
    evidence += [{"type": "reaction", "age_days_at_most": age(t)} for t, _ in dated["reactions"]]
    return facts, evidence


# ---------- fetch ----------

def estimate_cost(n, cfg):
    f, p = cfg["fetch"], cfg["prices_usd_per_1000"]
    per = {"posts": f["max_posts"] * p["posts"], "comments": f["max_comments"] * p["comments"],
           "reactions": f["max_reactions"] * p["reactions"]}
    return {k: n * v / 1000 for k, v in per.items()}


def fetch(client, urls, cfg, raw_dir):
    """Run the 3 actors per chunk of profiles. Returns ({kind: [items]}, {kind: error str})."""
    f = cfg["fetch"]
    items, errors = {k: [] for k in ACTORS}, {}

    def run(kind, chunk):
        inp = ({"targetUrls": chunk, "maxPosts": f["max_posts"], "includeReposts": f["include_reposts"],
                "includeQuotePosts": f["include_quote_posts"]} if kind == "posts"
               else {"profiles": chunk, "maxItems": f[f"max_{kind}"]})
        cap = Decimal(str(round(estimate_cost(len(chunk), cfg)[kind] * 1.25 + 0.05, 2)))  # hard spend cap per run
        r = client.actor(ACTORS[kind]).call(run_input=inp, max_total_charge_usd=cap, logger=None)
        return client.dataset(r.default_dataset_id).list_items().items

    chunks = [urls[i:i + f["chunk_size"]] for i in range(0, len(urls), f["chunk_size"])]
    with ThreadPoolExecutor(3) as ex:
        for chunk in chunks:
            futs = {k: ex.submit(run, k, chunk) for k in ACTORS}
            for k, fut in futs.items():
                try:
                    items[k] += fut.result()
                except Exception as e:  # one actor failing must not lose the other two
                    errors[k] = f"{type(e).__name__}: {e}"
                    print(f"[fetch] {k} failed: {errors[k]}", file=sys.stderr)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for k, v in items.items():
        (raw_dir / f"{k}.json").write_text(json.dumps(v, default=str))
    (raw_dir / "errors.json").write_text(json.dumps(errors))
    return items, errors


# ---------- judge (TypeSafe) ----------

def build_questions(cfg):
    from typesafe_sdk import Choice, Noul, Score
    qs = {}
    for qid, q in cfg["questions"].items():
        if q["type"] == "choice":
            qs[qid] = Choice(instructions=q["instructions"], criteria=q["criteria"])
        elif q["type"] == "score":
            qs[qid] = Score(instructions=q["instructions"], criteria=q["criteria"])
        else:
            qs[qid] = Noul(instructions=q["instructions"])
    return qs


def judge(ts, questions, facts, evidence, cfg):
    # counts only: the date-rule status and active_* flags would hand Jev the answer
    summary = {k: v for k, v in facts.items() if "_count" in k}
    r = ts.system_one(state={"summary": summary, "evidence": evidence}, questions=questions,
                      model=cfg["judge"]["model"])
    def label(qid):  # score is a float position on the levels; nearest level's label
        labels = cfg["questions"][qid]["labels"]
        return labels[min(len(labels) - 1, max(0, round(r.scores[qid].score)))]

    p =r.nouls["linkedin_outreach_ready"].noul
    status = r.choices["activity_status"]
    return {
        "activity_status": status.choice,
        "typesafe_status_confidence": round(status.confidence, 3),
        "activity_level": label("activity_level"),
        "activity_confidence": label("activity_confidence"),
        "outreach_ready_probability": round(p, 3),
        "linkedin_outreach_ready": p >= cfg["judge"]["outreach_ready_threshold"],
        "status_matches_date_rule": status.choice == facts["date_rule_status"],
    }


# ---------- assemble ----------

def explain(row, now):
    """Plain-English reason for the outreach verdict, assembled from facts + Jev answers (TypeSafe cannot write text)."""
    n, kind = row["days_since_last_activity"], row["last_activity_type"]
    if n is None:
        return "No public posts, comments or reactions were observed, so there is no sign this person uses LinkedIn."
    when = "today" if n == 0 else f"{n}d ago"
    last = (f"a reaction on a post from {when} (so the reaction is at most {max(n, 1)}d old)" if kind == "reaction"
            else f"a {kind} {when}")
    posts, reposts, comments, reacts = (row["recent_post_evidence_count_30d"], row["recent_repost_evidence_count_30d"],
                                        row["recent_comment_evidence_count_30d"], row["recent_reaction_evidence_count_30d"])
    parts = [f"{'Worth' if row['linkedin_outreach_ready'] else 'Not worth'} reaching out on LinkedIn now "
             f"(probability {row['outreach_ready_probability']:.2f}).",
             f"Status {row['activity_status']}: most recent evidence is {last}.",
             f"Last 30 days in sample: {posts} own posts, {reposts} reposts, {comments} comments, {reacts} reactions."]
    if posts:
        parts.append("Publishes their own posts.")
    elif reposts or comments or reacts:
        own = row["most_recent_observed_authored_post_at"]
        ago = f"last own post {(now - datetime.fromisoformat(own)).days}d ago" if own else "no own posts observed"
        parts.append(f"Engages with others' content rather than posting ({ago}).")
    parts.append(f"Activity level {row['activity_level']}; evidence confidence {row['activity_confidence']}"
                 + (" (reaction dates are lower bounds)." if kind == "reaction" else "."))
    return " ".join(parts)


def analyze(url, grouped, errors, now, cfg, ts, questions):
    u = username(url)
    facts, evidence = build_evidence(*(grouped[k].get(u, []) for k in ACTORS), now, cfg["windows_days"])
    row = dict.fromkeys(FIELDS) | {"linkedin_url": url, "username": u, "analyzed_at": now.isoformat(timespec="seconds"),
                                   "success": len(errors) < len(ACTORS), "status_code": 502 if errors else 200,
                                   "data_quality_warning": bool(errors)}
    row |= {k: v for k, v in facts.items() if k in FIELDS}
    if errors:
        row["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
    if not evidence and errors:  # a failed fetch is "unknown", not "no activity"
        return row
    if not evidence:  # nothing to judge
        row |= {"activity_status": "no_activity_observed", "activity_level": "none",
                "activity_confidence": "low", "linkedin_outreach_ready": False}
        return row | {"outreach_reason": explain(row, now)}
    try:
        row |= judge(ts, questions, facts, evidence, cfg)
        return row | {"outreach_reason": explain(row, now)}
    except Exception as e:
        return row | {"data_quality_warning": True, "error": f"typesafe: {type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="CSV with linkedin_url column, or TXT with one URL per line")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--out", default="out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true", help="print profile count + max Apify cost, then exit")
    ap.add_argument("--from-raw", help="re-judge cached actor output from out/raw/<run> without calling Apify")
    a = ap.parse_args()

    cfg = tomllib.loads(Path(a.config).read_text())
    urls = load_urls(a.input)[:a.limit]
    if not urls:
        sys.exit("no LinkedIn /in/ URLs found in input")
    est = estimate_cost(len(urls), cfg)
    if not a.from_raw:
        print(f"{len(urls)} profiles | max Apify cost ${sum(est.values()):.2f} "
              f"({', '.join(f'{k} ${v:.2f}' for k, v in est.items())})", file=sys.stderr)
    if a.dry_run:
        return
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY not set (put it in .env and run with: uv run --env-file .env ...)")

    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out)
    if a.from_raw:
        raw = Path(a.from_raw)
        items = {k: json.loads((raw / f"{k}.json").read_text()) for k in ACTORS}
        errors = json.loads((raw / "errors.json").read_text())
    else:
        token = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
        if not token:
            sys.exit("APIFY_TOKEN not set (put it in .env and run with: uv run --env-file .env ...)")
        from apify_client import ApifyClient
        items, errors = fetch(ApifyClient(token), urls, cfg, out / "raw" / run_id)

    users = [username(u) for u in urls]
    grouped = {}
    for k in ACTORS:
        grouped[k], missed = attribute(k, items[k], users)
        if missed:
            print(f"[attribute] {missed}/{len(items[k])} {k} items matched no input profile", file=sys.stderr)

    from typesafe_sdk import TypeSafeClient
    with TypeSafeClient() as ts, ThreadPoolExecutor(cfg["judge"]["workers"]) as ex:
        questions = build_questions(cfg)
        rows = list(ex.map(lambda url: analyze(url, grouped, errors, now, cfg, ts, questions), urls))

    out.mkdir(parents=True, exist_ok=True)
    (out / f"{run_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with open(out / f"{run_id}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[r["activity_status"]] = by.get(r["activity_status"], 0) + 1
    print(f"wrote {out / run_id}.jsonl + .csv | {by}", file=sys.stderr)


if __name__ == "__main__":
    main()
