import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from activity_intel import (ACTORS, FIELDS, WINDOWS, analyze, attribute, build_evidence, estimate_cost, explain,
                            fetch, load_raw, load_urls, run_cap, username)

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
WIN = WINDOWS
CFG = {"fetch": {"max_posts": 5, "max_comments": 5, "max_reactions": 10, "chunk_size": 50,
                 "include_reposts": True, "include_quote_posts": True},
       "prices_usd_per_1000": {"posts": 1.5, "comments": 2.0, "reactions": 2.0},
       "judge": {"model": "jev-latest", "outreach_ready_threshold": 0.5, "workers": 1},
       "questions": {"activity_level": {"labels": ["minimal", "low", "moderate", "high"]},
                     "activity_confidence": {"labels": ["low", "medium", "high"]}}}
EMPTY = {k: {} for k in ACTORS}
ms = lambda days: int((NOW - timedelta(days=days)).timestamp() * 1000)

post = lambda days, who="jane": {"type": "post", "linkedinUrl": f"https://lnkd/p{days}",
                                 "author": {"publicIdentifier": who}, "postedAt": {"timestamp": ms(days)}}
comment = lambda days, who="jane": {"linkedinUrl": f"https://lnkd/c{days}", "createdAtTimestamp": ms(days),
                                    "actor": {"linkedinUrl": f"https://www.linkedin.com/in/{who}?miniProfileUrn=x"}}
reaction = lambda days, who="jane": {"action": "likes this", "actor": {"linkedinUrl": f"https://www.linkedin.com/in/{who}?x=1"},
                                     "post": {"linkedinUrl": f"https://lnkd/rp{days}", "author": {"publicIdentifier": "someone-else"},
                                              "postedAt": {"timestamp": ms(days)}}}


class FakeTS:
    """Fixed TypeSafe answers; records the state it was sent."""
    def __init__(self, status="active", level=3.0, conf=2.0, noul=0.9, raise_=None):
        self.a, self.raise_, self.seen = (status, level, conf, noul), raise_, {}

    def system_one(self, state, questions, model):
        if self.raise_:
            raise self.raise_
        self.seen.update(state=state, model=model)
        s, l, c, n = self.a
        return NS(choices={"activity_status": NS(choice=s, confidence=0.9)},
                  scores={"activity_level": NS(score=l), "activity_confidence": NS(score=c)},
                  nouls={"linkedin_outreach_ready": NS(noul=n)})


class FakeApify:
    """client.actor(name).call(...) -> run; client.dataset(id).list_items().items; client.run(id).log().get().
    `plan` maps kind -> (status, items) or Exception; `logs` maps kind -> actor log text."""
    def __init__(self, plan, logs=None):
        self.plan, self.calls, self.logs = plan, [], logs or {}

    def actor(self, name):
        kind = next(k for k, v in ACTORS.items() if v == name)
        def call(run_input, max_total_charge_usd, logger):
            self.calls.append((kind, run_input, max_total_charge_usd))
            got = self.plan[kind]
            if isinstance(got, Exception):
                raise got
            status, items = got
            self.data = getattr(self, "data", {}) | {kind: items}
            return NS(id=f"run-{kind}", status=status, status_message="msg", charged_event_counts={"post": len(items)},
                      default_dataset_id=kind)
        return NS(call=call)

    def run(self, id_):
        kind = id_.removeprefix("run-")
        return NS(log=lambda: NS(get=lambda: self.logs.get(kind, "")),
                  get=lambda: NS(charged_event_counts={"post": len(self.data[kind])}, usage_total_usd=0.01))

    def dataset(self, id_):
        return NS(list_items=lambda: NS(items=self.data[id_]))


def grouped_for(u, posts=(), comments=(), reactions=()):
    return {"posts": {u: list(posts)}, "comments": {u: list(comments)}, "reactions": {u: list(reactions)}}


def test_username_and_load(tmp_path):
    assert username("https://www.linkedin.com/in/Jane-Doe/?utm=1") == "jane-doe"
    assert username("https://linkedin.com/company/x") is None
    f = tmp_path / "l.csv"
    f.write_text("name,linkedin_url\nA,https://www.linkedin.com/in/a/\nB,https://linkedin.com/in/A\nC,nope\n")
    assert load_urls(f) == ["https://www.linkedin.com/in/a"]
    t = tmp_path / "l.txt"
    t.write_text("https://www.linkedin.com/in/a\nhttps://www.linkedin.com/in/b\n")
    assert len(load_urls(t)) == 2


def test_attribute_by_actor_not_post_author():
    got, missed = attribute("reactions", [reaction(3), reaction(3, "bob"), reaction(3, "stranger")], ["jane", "bob"])
    assert len(got["jane"]) == 1 and len(got["bob"]) == 1 and missed == 1
    got, missed = attribute("posts", [post(1), post(1, "original-author")], ["jane"])
    assert len(got["jane"]) == 1 and missed == 1


def test_reaction_only_lurker_is_active_via_lower_bound():
    facts, ev = build_evidence([], [], [reaction(5), reaction(20), reaction(200)], NOW, WIN)
    assert facts["active_7d"] and facts["date_rule_status"] == "active"
    assert facts["most_recent_observed_authored_post_at"] is None and facts["most_recent_exact_activity_at"] is None
    assert (facts["recent_reaction_evidence_count_7d"], facts["recent_reaction_evidence_count_30d"],
            facts["recent_reaction_evidence_count_90d"], facts["reaction_evidence_count"]) == (1, 2, 2, 3)
    assert ev[0] == {"type": "reaction", "age_days_at_most": 5}


def test_window_edges_and_statuses():
    status = lambda d: build_evidence([post(d)], [], [], NOW, WIN)[0]["date_rule_status"]
    assert [status(d) for d in (30, 31, 90, 91)] == ["active", "stale", "stale", "inactive"]
    assert build_evidence([], [], [], NOW, WIN)[0]["date_rule_status"] == "no_activity_observed"


def test_newest_wins_regardless_of_item_order():
    facts, _ = build_evidence([post(100), post(2)], [comment(40)], [], NOW, WIN)
    assert facts["most_recent_post_url"] == "https://lnkd/p2"
    assert facts["recent_activity_evidence_at"] == facts["most_recent_observed_authored_post_at"]
    assert facts["most_recent_observed_authored_comment_at"].startswith("2026-08-13")


def test_analyze_without_evidence_never_calls_typesafe():
    row = analyze("https://www.linkedin.com/in/jane", EMPTY, [], NOW, CFG, None, None)
    assert row["activity_status"] == "no_activity_observed" and row["success"] and not row["data_quality_warning"]
    runs = [{"kind": k, "profiles": ["https://www.linkedin.com/in/jane"], "error": "x"} for k in ACTORS]
    failed = analyze("https://www.linkedin.com/in/jane", EMPTY, runs, NOW, CFG, None, None)
    assert failed["activity_status"] is None and not failed["success"] and failed["data_quality_warning"]


def test_judge_maps_typesafe_answers_with_real_config():
    import tomllib
    from pathlib import Path
    from activity_intel import build_questions, judge

    cfg = tomllib.loads(Path("config.toml").read_text())
    cfg["judge"]["outreach_ready_threshold"] = 0.5  # pinned: the test must not depend on a user-owned value
    questions = build_questions(cfg)  # validates config against the real SDK question types
    assert set(questions) == {"activity_status", "activity_level", "activity_confidence", "linkedin_outreach_ready"}
    ts = FakeTS(status="stale", level=2.4, conf=1.6, noul=0.49)
    facts, ev = build_evidence([post(3)], [], [reaction(1)], NOW, WIN)
    got = judge(ts, questions, facts, ev, cfg)
    assert got["activity_level"] == "moderate" and got["activity_confidence"] == "high"
    assert got["linkedin_outreach_ready"] is False and got["status_matches_date_rule"] is False  # rule says active
    assert all("_count" in k for k in ts.seen["state"]["summary"]) and ts.seen["model"] == "jev-latest"  # no answer leakage
    assert "rejected_evidence_count" not in ts.seen["state"]["summary"]


def test_explain_reaction_only_lurker_and_no_evidence():
    facts, _ = build_evidence([post(430)], [comment(12)], [reaction(3), reaction(20)], NOW, WIN)
    assert (facts["days_since_last_activity"], facts["last_activity_type"]) == (3, "reaction")
    row = dict.fromkeys(FIELDS) | facts | {"linkedin_outreach_ready": True, "outreach_ready_probability": 0.71,
                                           "activity_status": "active", "activity_level": "moderate", "activity_confidence": "medium"}
    text = explain(row, NOW)
    assert text.startswith("Worth reaching out") and "at most 3d old" in text
    assert "0 own posts, 0 reposts, 1 comments, 2 reactions" in text and "last own post 430d ago" in text and "lower bounds" in text
    empty = analyze("https://www.linkedin.com/in/jane", EMPTY, [], NOW, CFG, None, None)
    assert "No public posts" in empty["outreach_reason"]


def test_repost_is_activity_but_not_an_authored_post():
    repost = post(2, "original-author") | {"query": {"targetUrl": "https://www.linkedin.com/in/jane"},
                                           "repostedBy": {"publicIdentifier": "jane"}}
    got, missed = attribute("posts", [repost], ["jane"])
    assert len(got["jane"]) == 1 and missed == 0
    facts, ev = build_evidence([repost, post(400)], [], [], NOW, WIN)
    assert facts["active_7d"] and facts["last_activity_type"] == "repost"
    assert (facts["post_evidence_count"], facts["repost_evidence_count"], facts["recent_post_evidence_count_30d"]) == (1, 1, 0)
    assert facts["most_recent_observed_authored_post_at"].startswith("2025-08")  # the 400d-old own post, not the repost
    assert {"type": "repost", "age_days": 2} in ev


# ---------- MVP review reproductions ----------

def test_failed_apify_run_is_unknown_not_no_activity(tmp_path):
    client = FakeApify({"posts": ("FAILED", []), "comments": ("SUCCEEDED", []), "reactions": ("TIMED-OUT", [])})
    url = "https://www.linkedin.com/in/jane"
    items, runs = fetch(client, [url], CFG, tmp_path / "raw", NOW)
    assert [r["error"] is not None for r in runs] == [True, False, True] and runs[0]["run_id"] == "run-posts"
    m = json.loads((tmp_path / "raw" / "manifest.json").read_text())
    assert m["profiles"] == [url] and len(m["runs"]) == 3 and m["config"]["fetch"]["max_posts"] == 5
    assert m["runs"][1]["charged_events"] == {"post": 0}
    row = analyze(url, EMPTY, runs, NOW, CFG, None, None)
    assert row["success"] and row["status_code"] == 502 and row["activity_status"] is None and "FAILED" in row["error"]


def test_malformed_timestamp_is_rejected_not_fatal():
    bad = {"linkedinUrl": "https://lnkd/bad", "createdAt": "bad-date"}
    facts, ev = build_evidence([post(3)], [bad, comment(5)], [], NOW, WIN)
    assert facts["rejected_evidence_count"] == 1 and facts["comment_evidence_count"] == 1 and len(ev) == 2
    row = analyze("https://www.linkedin.com/in/jane", grouped_for("jane", [post(3)], [bad]), [], NOW, CFG, FakeTS(), None)
    assert row["success"] and row["data_quality_warning"] and row["rejected_evidence_count"] == 1
    # anything unexpected inside analyze() becomes a row, never an exception that kills the batch
    row = analyze("https://www.linkedin.com/in/jane", {"posts": None}, [], NOW, CFG, None, None)
    assert row["success"] is False and row["status_code"] == 500 and "AttributeError" in row["error"]


def test_fetch_errors_are_per_chunk_not_global():
    jane, bob = "https://www.linkedin.com/in/jane", "https://www.linkedin.com/in/bob"
    runs = [{"kind": "posts", "profiles": [jane], "error": "run x FAILED: msg"}]
    j = analyze(jane, EMPTY, runs, NOW, CFG, None, None)
    b = analyze(bob, EMPTY, runs, NOW, CFG, None, None)
    assert j["status_code"] == 502 and j["activity_status"] is None
    assert b["status_code"] == 200 and b["success"] and b["activity_status"] == "no_activity_observed"
    # partial evidence + a failed source: Jev is told which source is missing
    ts = FakeTS()
    analyze(jane, grouped_for("jane", comments=[comment(2)]), runs, NOW, CFG, ts, None)
    assert ts.seen["state"]["summary"]["sources_unavailable"] == ["posts"]


def test_typesafe_failure_is_a_failed_row_with_both_errors():
    jane = "https://www.linkedin.com/in/jane"
    runs = [{"kind": "reactions", "profiles": [jane], "error": "run r ABORTED: msg"}]
    row = analyze(jane, grouped_for("jane", [post(1)]), runs, NOW, CFG, FakeTS(raise_=TimeoutError("slow")), None)
    assert row["success"] is False and row["status_code"] == 502
    assert "reactions: run r ABORTED" in row["error"] and "typesafe: TimeoutError" in row["error"]


def test_replay_never_reassigns_someone_elses_item():
    bobs_repost = post(2, "jane") | {"query": {"targetUrl": "https://www.linkedin.com/in/bob"},
                                     "repostedBy": {"publicIdentifier": "bob"}}
    got, missed = attribute("posts", [bobs_repost], ["jane"])  # replaying only Jane
    assert got["jane"] == [] and missed == 1
    got, missed = attribute("posts", [bobs_repost], ["jane", "bob"])
    assert len(got["bob"]) == 1 and missed == 0


def test_from_raw_flags_profiles_the_cache_never_collected(tmp_path):
    jane, bob = "https://www.linkedin.com/in/jane", "https://www.linkedin.com/in/bob"
    client = FakeApify({k: ("SUCCEEDED", []) for k in ACTORS})
    fetch(client, [jane], CFG, tmp_path, NOW)
    items, runs = load_raw(tmp_path, [jane, bob])
    assert analyze(jane, EMPTY, runs, NOW, CFG, None, None)["activity_status"] == "no_activity_observed"
    b = analyze(bob, EMPTY, runs, NOW, CFG, None, None)
    assert b["success"] is False and b["status_code"] == 502 and "not collected" in b["error"]


def test_fractional_ages_and_future_timestamps():
    facts, ev = build_evidence([post(30.9)], [], [], NOW, WIN)
    assert facts["date_rule_status"] == "stale" and facts["active_30d"] is False and abs(ev[0]["age_days"] - 30.9) < 1e-6
    assert build_evidence([post(90.9)], [], [], NOW, WIN)[0]["date_rule_status"] == "inactive"
    facts, ev = build_evidence([], [], [reaction(3.9)], NOW, WIN)
    assert abs(ev[0]["age_days_at_most"] - 3.9) < 1e-6 and facts["active_7d"]
    row = dict.fromkeys(FIELDS) | facts | {"linkedin_outreach_ready": True, "outreach_ready_probability": 0.8,
                                           "activity_status": "active", "activity_level": "low", "activity_confidence": "low"}
    assert "at most 4d old" in explain(row, NOW)
    # the reviewer's cases: a 3.04d bound must not be shown as 3.0; a 30.04d post must reach Jev as > 30, not 30.0
    facts, ev = build_evidence([], [], [reaction(3.04)], NOW, WIN)
    assert facts["days_since_last_activity"] == 3.1 and ev[0]["age_days_at_most"] > 3.0
    facts, ev = build_evidence([post(30.04)], [], [], NOW, WIN)
    assert facts["days_since_last_activity"] == 30.0 and ev[0]["age_days"] > 30 and facts["date_rule_status"] == "stale"
    facts, _ = build_evidence([post(-2)], [], [reaction(0.2)], NOW, WIN)  # post dated 2 days in the future
    assert facts["rejected_evidence_count"] == 1 and facts["post_evidence_count"] == 0 and facts["last_activity_type"] == "reaction"
    assert "today" in explain(dict.fromkeys(FIELDS) | facts | {"linkedin_outreach_ready": True, "outreach_ready_probability": 0.8,
                                                                "activity_status": "active", "activity_level": "low",
                                                                "activity_confidence": "low"}, NOW)


def test_dry_run_reports_estimate_and_authorized_caps_separately():
    est = estimate_cost(1, CFG)
    caps = sum(run_cap(1, k, CFG) for k in ACTORS)
    assert round(sum(est.values()), 4) == 0.0375 and float(caps) == 0.20  # the reviewer's numbers
    client = FakeApify({k: ("SUCCEEDED", []) for k in ACTORS})
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        fetch(client, ["https://www.linkedin.com/in/jane"], CFG, Path(d), NOW)
    assert sum(c[2] for c in client.calls) == caps  # fetch() authorizes exactly what --dry-run prints


def test_reaction_only_confidence_is_capped_at_medium():
    row = analyze("https://www.linkedin.com/in/jane", grouped_for("jane", reactions=[reaction(1), reaction(2)]), [],
                  NOW, CFG, FakeTS(conf=2.0), None)
    assert row["activity_confidence"] == "medium"
    row = analyze("https://www.linkedin.com/in/jane", grouped_for("jane", [post(1)], reactions=[reaction(1)]), [],
                  NOW, CFG, FakeTS(conf=2.0), None)
    assert row["activity_confidence"] == "high"


def test_latest_activity_metadata_and_sample_caps():
    caps = {"posts": 5, "comments": 5, "reactions": 10}
    facts, _ = build_evidence([post(10)], [comment(d) for d in (4, 5, 6, 7, 8)], [reaction(1)], NOW, WIN, caps)
    assert facts["most_recent_activity_url"] == "https://lnkd/rp1" and facts["last_activity_type"] == "reaction"
    assert facts["most_recent_exact_activity_at"].startswith("2026-09-18")  # the 4d-old comment, not the reaction bound
    assert facts["samples_at_cap"] == "comments"
    facts, _ = build_evidence([post(3)], [], [], NOW, WIN, caps)
    assert facts["most_recent_activity_url"] == "https://lnkd/p3" and facts["samples_at_cap"] == ""


def test_succeeded_run_that_skipped_profiles_in_its_log_marks_them_failed(tmp_path):
    jane, bob = "https://www.linkedin.com/in/jane", "https://www.linkedin.com/in/bob"
    log = ('2026-09-22T03:19:26.053Z Fetching posts for {"targetUrl":"https://www.linkedin.com/in/bob"}...\n'
           '2026-09-22T03:19:26.062Z Error scraping item#1 {"targetUrl":"https://www.linkedin.com/in/bob"}: "Too many queued requests (code_22)"\n'
           '2026-09-22T03:19:26.062Z Scraped posts for {"targetUrl":"https://www.linkedin.com/in/bob"}. Posts found 0. Progress: 2/10\n')
    client = FakeApify({"posts": ("SUCCEEDED", [post(1) | {"query": {"targetUrl": jane}}]),
                        "comments": ("SUCCEEDED", []), "reactions": ("SUCCEEDED", [])}, logs={"posts": log})
    items, runs = fetch(client, [jane, bob], CFG, tmp_path, NOW)
    assert [c[1].get("targetUrls") for c in client.calls if c[0] == "posts"] == [[jane, bob], [bob]]  # one retry, bob only
    skipped = [r for r in runs if r["error"]]
    assert len(skipped) == 1 and skipped[0]["profiles"] == [bob] and "code_22" in skipped[0]["error"]
    assert analyze(jane, {"posts": {"jane": items["posts"]}, "comments": {}, "reactions": {}}, runs, NOW, CFG, FakeTS(), None)["status_code"] == 200
    b = analyze(bob, EMPTY, runs, NOW, CFG, None, None)
    assert b["status_code"] == 502 and b["activity_status"] is None and "posts: actor errors" in b["error"]


def test_retry_or_metadata_failure_never_discards_the_first_runs_results(tmp_path):
    jane, bob = "https://www.linkedin.com/in/jane", "https://www.linkedin.com/in/bob"
    log = '2026-09-22T03:19:26.062Z Error scraping item#1 {"targetUrl":"https://www.linkedin.com/in/bob"}: "Too many queued requests (code_22)"\n'
    janes = [post(1) | {"query": {"targetUrl": jane}}]

    class Flaky(FakeApify):  # first posts call succeeds, the retry raises
        def actor(self, name):
            inner = super().actor(name)
            def call(**kw):
                if kw["run_input"].get("targetUrls") == [bob]:
                    raise TimeoutError("retry timed out")
                return inner.call(**kw)
            return NS(call=call)

    client = Flaky({"posts": ("SUCCEEDED", janes), "comments": ("SUCCEEDED", []), "reactions": ("SUCCEEDED", [])}, logs={"posts": log})
    items, runs = fetch(client, [jane, bob], CFG, tmp_path / "a", NOW)
    assert items["posts"] == janes and [r["run_id"] for r in runs if r["kind"] == "posts"] == ["run-posts", "run-posts"]
    err = [r for r in runs if r["error"]]
    assert len(err) == 1 and err[0]["profiles"] == [bob] and "retry failed: TimeoutError" in err[0]["error"]

    class NoMeta(FakeApify):  # run record and log lookups fail; the scrape itself is fine
        def run(self, id_):
            raise TimeoutError("meta")

    client = NoMeta({"posts": ("SUCCEEDED", janes), "comments": ("SUCCEEDED", []), "reactions": ("SUCCEEDED", [])})
    items, runs = fetch(client, [jane], CFG, tmp_path / "b", NOW)
    assert items["posts"] == janes and all(r["error"] is None for r in runs)
    assert runs[0]["charges_final"] is False and runs[0]["charged_events"] is None and "TimeoutError" in runs[0]["charges_error"]
    assert "TimeoutError" in runs[0]["log_error"] and runs[0]["charges_observed_at"]
