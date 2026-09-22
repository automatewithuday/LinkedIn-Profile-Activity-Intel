from datetime import datetime, timedelta, timezone

from activity_intel import FIELDS, analyze, attribute, build_evidence, explain, load_urls, username

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
WIN = {"recent": 7, "active": 30, "stale": 90}
ms = lambda days: int((NOW - timedelta(days=days)).timestamp() * 1000)

post = lambda days, who="jane": {"type": "post", "linkedinUrl": f"https://lnkd/p{days}",
                                 "author": {"publicIdentifier": who}, "postedAt": {"timestamp": ms(days)}}
comment = lambda days, who="jane": {"linkedinUrl": f"https://lnkd/c{days}", "createdAtTimestamp": ms(days),
                                    "actor": {"linkedinUrl": f"https://www.linkedin.com/in/{who}?miniProfileUrn=x"}}
reaction = lambda days, who="jane": {"action": "likes this", "actor": {"linkedinUrl": f"https://www.linkedin.com/in/{who}?x=1"},
                                     "post": {"author": {"publicIdentifier": "someone-else"}, "postedAt": {"timestamp": ms(days)}}}


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
    assert facts["most_recent_observed_authored_post_at"] is None
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
    cfg = {"windows_days": WIN}
    empty = {k: {} for k in ("posts", "comments", "reactions")}
    row = analyze("https://www.linkedin.com/in/jane", empty, {}, NOW, cfg, None, None)
    assert row["activity_status"] == "no_activity_observed" and row["success"] and not row["data_quality_warning"]
    failed = analyze("https://www.linkedin.com/in/jane", empty, {"posts": "x", "comments": "x", "reactions": "x"}, NOW, cfg, None, None)
    assert failed["activity_status"] is None and not failed["success"] and failed["data_quality_warning"]


def test_judge_maps_typesafe_answers_with_real_config():
    import tomllib
    from pathlib import Path
    from types import SimpleNamespace as NS
    from activity_intel import build_questions, judge

    cfg = tomllib.loads(Path("config.toml").read_text())
    cfg["judge"]["outreach_ready_threshold"] = 0.5  # pinned: the test must not depend on a user-owned value
    questions = build_questions(cfg)  # validates config against the real SDK question types
    assert set(questions) == {"activity_status", "activity_level", "activity_confidence", "linkedin_outreach_ready"}
    seen = {}

    class FakeTS:
        def system_one(self, state, questions, model):
            seen.update(state=state, model=model)
            return NS(choices={"activity_status": NS(choice="stale", confidence=0.91)},
                      scores={"activity_level": NS(score=2.4), "activity_confidence": NS(score=1.6)},
                      nouls={"linkedin_outreach_ready": NS(noul=0.49)})

    facts, ev = build_evidence([post(3)], [], [reaction(1)], NOW, WIN)
    got = judge(FakeTS(), questions, facts, ev, cfg)
    assert got["activity_level"] == "moderate" and got["activity_confidence"] == "high"
    assert got["linkedin_outreach_ready"] is False and got["status_matches_date_rule"] is False  # rule says active
    assert all("_count" in k for k in seen["state"]["summary"]) and seen["model"] == "jev-latest"  # no answer leakage


def test_explain_reaction_only_lurker_and_no_evidence():
    facts, _ = build_evidence([post(430)], [comment(12)], [reaction(3), reaction(20)], NOW, WIN)
    assert (facts["days_since_last_activity"], facts["last_activity_type"]) == (3, "reaction")
    row = dict.fromkeys(FIELDS) | facts | {"linkedin_outreach_ready": True, "outreach_ready_probability": 0.71,
                                           "activity_status": "active", "activity_level": "moderate", "activity_confidence": "medium"}
    text = explain(row, NOW)
    assert text.startswith("Worth reaching out") and "at most 3d old" in text
    assert "0 own posts, 0 reposts, 1 comments, 2 reactions" in text and "last own post 430d ago" in text and "lower bounds" in text
    empty = analyze("https://www.linkedin.com/in/jane", {k: {} for k in ("posts", "comments", "reactions")}, {}, NOW,
                    {"windows_days": WIN}, None, None)
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
