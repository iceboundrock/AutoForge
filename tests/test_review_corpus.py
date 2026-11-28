"""Offline tests for REV-R1 review corpus schema, git verifier, and scorer (#356)."""

from __future__ import annotations

import json
import os
import subprocess
import tracemalloc
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from tests import review_corpus as rc
from tests.conftest import GIT_IDENT, NO_AUTO_MAINTENANCE

REPO_ROOT = Path(__file__).resolve().parents[1]


# -- helpers ------------------------------------------------------------------


def _git_env(dt: str = "2026-01-01T00:00:00Z") -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "AutoForge Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "AutoForge Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": dt,
        "GIT_COMMITTER_DATE": dt,
    }


def _git(repo: Path, *args: str, dt: str = "2026-01-01T00:00:00Z") -> str:
    done = subprocess.run(
        ["git", *NO_AUTO_MAINTENANCE, *GIT_IDENT, "-C", str(repo), *args],
        env=_git_env(dt),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _item(
    item_id: str,
    kind: str,
    source: str | None,
    text: str | None,
    *,
    observed_at: str | None = "2026-01-03T00:00:00Z",
    edited_at: str | None = None,
    ref: str = "https://example.com/item",
) -> dict:
    if text is None:
        return {
            "id": item_id,
            "kind": kind,
            "source": source,
            "ref": ref,
            "observed_at": None,
            "edited_at": None,
            "status": "not_captured",
            "text": None,
            "sha256": None,
        }
    return {
        "id": item_id,
        "kind": kind,
        "source": source,
        "ref": ref,
        "observed_at": observed_at,
        "edited_at": edited_at,
        "status": "captured",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
    }


def _label(
    by: str,
    verdict: str,
    *,
    role: str = "adjudicator",
    decided: str = "2026-01-10",
    artifact: str = "reproducible artifact",
    defect: str | None = None,
    with_defect: bool = False,
) -> dict:
    row: dict = {
        "by": by,
        "role": role,
        "verdict": verdict,
        "decided": decided,
        "artifact": artifact,
    }
    if with_defect:
        row["defect"] = defect
    return row


def _variant(
    identity: str = "v1",
    *,
    model: str = "m1",
    knowledge_cutoff: str | None = "2025-12-01",
    controller: str | None = None,
) -> dict:
    return {
        "identity": identity,
        "provider": "fake-provider",
        "provider_version": "1.0",
        "model": model,
        "effort": "high",
        "knowledge_cutoff": knowledge_cutoff,
        "prompt": {
            "template": "src/autoforge/prompts/review.md",
            "sha256": "a" * 64,
        },
        "controller": controller or ("1" * 40),
        "sandbox": "required",
    }


def _make_hist_repo(tmp_path: Path) -> dict:
    """Create a real local git repository exercising pr_branch, squash, and merge_pr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("line1 = 1\nline2 = 2\nline3 = 3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "root", dt="2026-01-01T00:00:00Z")
    root = _git(repo, "rev-parse", "HEAD")

    # Base commit on main before PR branch forks
    (repo / "src/a.py").write_text("line1 = 1\nline2 = 2\nline3 = 3\nline4 = 4\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base work", dt="2026-01-02T00:00:00Z")
    base1 = _git(repo, "rev-parse", "HEAD")

    # PR #2 branch: introduces bug on line 2
    _git(repo, "checkout", "-b", "pr2", base1)
    (repo / "src/a.py").write_text(
        "line1 = 1\nline2 = 999  # bug\nline3 = 3\nline4 = 4\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: pr2 head", dt="2026-01-03T01:00:00Z")
    pr2_head = _git(repo, "rev-parse", "HEAD")

    # Branch fix commit after cutoff
    (repo / "src/a.py").write_text(
        "line1 = 1\nline2 = 2  # fixed\nline3 = 3\nline4 = 4\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: R1-F1", dt="2026-01-04T01:00:00Z")
    pr2_fix = _git(repo, "rev-parse", "HEAD")

    # Squash of PR #2 on main (carries bug on line 2), followed by post-merge fix
    _git(repo, "checkout", "main")
    (repo / "src/a.py").write_text(
        "line1 = 1\nline2 = 999  # bug\nline3 = 3\nline4 = 4\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: pr2 (#2)", dt="2026-01-06T00:00:00Z")
    squash2 = _git(repo, "rev-parse", "HEAD")

    (repo / "src/a.py").write_text(
        "line1 = 1\nline2 = 2  # post-merge fixed\nline3 = 3\nline4 = 4\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: post-merge pr2", dt="2026-01-08T00:00:00Z")
    post_fix2 = _git(repo, "rev-parse", "HEAD")

    # PR #3 as a 2-parent merge_pr on main, with a later revert on main
    _git(repo, "checkout", "-b", "pr3", post_fix2)
    (repo / "src/b.py").write_text("ok = True\nbad = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: pr3 head", dt="2026-01-10T01:00:00Z")
    pr3_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "main")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "docs update", dt="2026-01-10T02:00:00Z")
    pr3_base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "merge", "--no-ff", "-m", "Merge pull request #3", "pr3", dt="2026-01-11T00:00:00Z")
    merge3 = _git(repo, "rev-parse", "HEAD")

    _git(repo, "revert", "--no-edit", "-m", "1", merge3, dt="2026-01-12T00:00:00Z")
    revert3 = _git(repo, "rev-parse", "HEAD")
    main_tip = revert3

    def tree(oid: str) -> str:
        return _git(repo, "rev-parse", f"{oid}^{{tree}}")

    def blob(oid: str, path: str) -> str:
        return _git(repo, "rev-parse", f"{oid}:{path}")

    return {
        "repo": repo,
        "root": root,
        "base1": base1,
        "pr2_head": pr2_head,
        "pr2_fix": pr2_fix,
        "squash2": squash2,
        "post_fix2": post_fix2,
        "pr3_base": pr3_base,
        "pr3_head": pr3_head,
        "merge3": merge3,
        "revert3": revert3,
        "main_tip": main_tip,
        "tree": tree,
        "blob": blob,
    }


def _hist_branch_corpus(
    h: dict, *, cutoff: str = "2026-01-03T02:00:00Z", base_evidence: str = "recorded"
) -> dict:
    tree = h["tree"]
    blob = h["blob"]
    head = h["pr2_head"]
    base = h["base1"]
    return {
        "format": "autoforge-review-corpus",
        "version": 1,
        "mode": "historical",
        "name": "hist-test",
        "release": 1,
        "frozen": "2026-02-01",
        "repository": {
            "slug": "other/custom-repo",
            "root": h["root"],
            "refs": {"main": h["main_tip"]},
            "visibility": "public",
            "environment": {
                "files": ["pyproject.toml", "uv.lock"],
                "validation_scope": "full",
                "validation": ["make check"],
            },
        },
        "cases": {
            "pr2-r1": {
                "stratum": "reviewed_defect",
                "cluster": "cl-pr2",
                "provenance": {
                    "source": "pr_branch",
                    "issue": 1,
                    "pr": 2,
                    "round": 1,
                    "base_evidence": base_evidence,
                    "merge_commit": None,
                    "selection": "test",
                    "note": "",
                },
                "review": {
                    "base_ref": "main",
                    "base_tip": base,
                    "merge_base": base,
                    "head": head,
                    "base_tree": tree(base),
                    "merge_base_tree": tree(base),
                    "head_tree": tree(head),
                    "cutoff": cutoff,
                },
                "environment": {
                    "files": {
                        "pyproject.toml": blob(head, "pyproject.toml"),
                        "uv.lock": blob(head, "uv.lock"),
                    },
                    "validation_scope": "full",
                    "validation": ["make check"],
                },
                "context": {
                    "inventory": {
                        "status": "complete",
                        "taken": "2026-01-03T03:00:00Z",
                        "artifact": "inv",
                        "edit_history_verified": True,
                        "sources": ["issue/1", "pr/2"],
                    },
                    "items": [
                        _item(
                            "issue",
                            "issue",
                            "issue/1",
                            "Fix line2.",
                            observed_at="2026-01-02T12:00:00Z",
                        ),
                        _item(
                            "pr-body",
                            "pr_body",
                            "pr/2",
                            "Updates line2.",
                            observed_at="2026-01-03T01:30:00Z",
                        ),
                    ],
                },
                "actors": ["producer", "reviewer", "fixer"],
                "risk": {"stratum": "routine", "rationale": "test"},
                "exposure": {"possible_from": "2026-01-03", "public_by": None, "artifact": "git"},
                "defects": {
                    "d-pr2-1": {
                        "severity": "low",
                        "class": "correctness",
                        "introduced": head,
                        "summary": "wrong line2",
                        "location": [{"path": "src/a.py", "lines": [2, 2]}],
                        "discovery": {
                            "source": "same_review",
                            "round": 1,
                            "finding": "R1-F1",
                            "discovered": "2026-01-04",
                            "fix": h["pr2_fix"],
                            "merged": None,
                            "evidence_status": "recorded",
                            "artifact": "review round 1 + fix",
                        },
                        "labels": [_label("adj1", "confirmed", decided="2026-01-10")],
                    }
                },
                "negative": None,
                "withheld": [h["pr2_fix"]],
                "post_cutoff_refs": [],
            }
        },
    }


def _subs_for(corpus: dict, case_ids: list[str] | None = None) -> dict:
    checked = rc.check_corpus(corpus)
    mode = "synthetic" if checked.mode == "synthetic" else "recorded"
    cids = case_ids or sorted(checked.cases)
    v1 = _variant("rev-base", model="m1")
    v2 = _variant("rev-alt", model="m2")
    p1, p2 = rc.pins_digest(v1), rc.pins_digest(v2)
    runs = []
    for vname, pin in (("baseline", p1), ("variant-a", p2)):
        for cid in cids:
            runs.append(
                {
                    "variant": vname,
                    "case": cid,
                    "seed": 1,
                    "input": checked.cases[cid].input,
                    "pins_sha256": pin,
                    "status": "completed",
                    "exclusion": None,
                    "cost_microusd": 100,
                    "findings": [],
                }
            )
    return {
        "format": "autoforge-review-submissions",
        "version": 1,
        "mode": mode,
        "corpus": checked.digest,
        "frozen": "2026-02-15",
        "baseline": "baseline",
        "plan": {"cases": cids, "seeds": [1]},
        "variants": {"baseline": v1, "variant-a": v2},
        "runs": runs,
    }


@pytest.fixture
def syn_corpus() -> dict:
    return json.loads(rc.SYNTHETIC_CORPUS.read_bytes())


@pytest.fixture
def syn_subs() -> dict:
    return json.loads(rc.SYNTHETIC_SUBMISSIONS.read_bytes())


# -- A. Schema, closed keys & pins --------------------------------------------


def test_schema_closed_keys_and_pin_formats(syn_corpus, syn_subs):
    bad_top = deepcopy(syn_corpus)
    bad_top["extra"] = 1
    with pytest.raises(rc.CorpusError, match="fields"):
        rc.check_corpus(bad_top)

    bad_sha = deepcopy(syn_corpus)
    bad_sha["repository"]["root"] = "Z" * 40
    with pytest.raises(rc.CorpusError, match="object id shape"):
        rc.check_corpus(bad_sha)

    bad_time = deepcopy(syn_corpus)
    bad_time["cases"]["alpha-defect"]["review"]["cutoff"] = "2026-05-01T12:00:00"
    with pytest.raises(rc.CorpusError, match="instant shape"):
        rc.check_corpus(bad_time)

    bad_sub = deepcopy(syn_subs)
    bad_sub["runs"][0]["pins_sha256"] = "0" * 64
    with pytest.raises(rc.CorpusError, match="variant's pins"):
        rc.check_submissions(rc.check_corpus(syn_corpus), bad_sub)

    bad_input = deepcopy(syn_subs)
    bad_input["runs"][0]["input"] = "0" * 64
    with pytest.raises(rc.CorpusError, match="frozen input"):
        rc.check_submissions(rc.check_corpus(syn_corpus), bad_input)


def test_validation_scope_fast_refused_and_empty_validation_admitted(syn_corpus, tmp_path):
    fast = deepcopy(syn_corpus)
    fast["repository"]["environment"]["validation_scope"] = "fast"
    with pytest.raises(rc.CorpusError, match="FULL validation"):
        rc.check_corpus(fast)

    fast_case = deepcopy(syn_corpus)
    fast_case["cases"]["alpha-defect"]["environment"]["validation_scope"] = "fast"
    with pytest.raises(rc.CorpusError, match="FULL validation"):
        rc.check_corpus(fast_case)

    # Explicit empty validation list is admitted cleanly and works for non-AutoForge repos
    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    c["repository"]["environment"]["validation"] = []
    c["cases"]["pr2-r1"]["environment"]["validation"] = []
    checked = rc.check_corpus(c)
    assert checked.cases["pr2-r1"].runnable is True
    assert rc.verify(c, h["repo"])["ok"] is True


def test_all_null_environment_files_leave_case_unrunnable(syn_corpus):
    c = deepcopy(syn_corpus)
    c["cases"]["alpha-defect"]["environment"]["files"] = {"pyproject.toml": None, "uv.lock": None}
    checked = rc.check_corpus(c)
    assert checked.cases["alpha-defect"].runnable is False


def test_unverified_edit_history_leaves_case_unrunnable(syn_corpus, tmp_path):
    synthetic = deepcopy(syn_corpus)
    assert rc.check_corpus(synthetic).cases["alpha-defect"].runnable is True
    synthetic["cases"]["alpha-defect"]["context"]["inventory"]["edit_history_verified"] = False
    checked = rc.check_corpus(synthetic)
    assert checked.cases["alpha-defect"].runnable is False
    assert "alpha-defect" in rc.describe(synthetic)["unplanned"]

    # An otherwise runnable historical case is held to the same rule.
    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    assert rc.check_corpus(c).cases["pr2-r1"].runnable is True
    c["cases"]["pr2-r1"]["context"]["inventory"]["edit_history_verified"] = False
    assert rc.check_corpus(c).cases["pr2-r1"].runnable is False


# -- B. Reviewer-visible boundary & O(N) leakage ------------------------------


def test_project_reviewer_input_excludes_ref_and_ground_truth(syn_corpus):
    raw_case = syn_corpus["cases"]["alpha-defect"]
    # Putting a withheld SHA in curator metadata `ref` does NOT enter reviewer input!
    raw_case["context"]["items"][0]["ref"] = raw_case["withheld"][0]
    proj = rc.project_reviewer_input(raw_case)
    assert set(proj) == {"format", "version", "review", "environment", "context"}
    for item in proj["context"]:
        assert set(item) == {"id", "kind", "source", "text", "sha256"}
    # Still passes check_corpus because `ref` is outside reviewer input!
    d_with_sha = rc.check_corpus(syn_corpus).cases["alpha-defect"].input
    raw_case["context"]["items"][0]["ref"] = "other ref"
    assert rc.check_corpus(syn_corpus).cases["alpha-defect"].input == d_with_sha


def test_exported_input_alone_reproduces_the_frozen_context(syn_corpus, capsys):
    # The reviewer receives the captured snapshot bytes, bound by their digests,
    # so neither the private corpus nor live GitHub is needed to rebuild them.
    raw_case = syn_corpus["cases"]["alpha-defect"]
    assert rc.main(["export-input", str(rc.SYNTHETIC_CORPUS), "alpha-defect"]) == 0
    exported = json.loads(capsys.readouterr().out)
    frozen = {item["id"]: item["text"] for item in raw_case["context"]["items"]}
    assert frozen and all(text is not None for text in frozen.values())
    rebuilt = {item["id"]: item["text"] for item in exported["context"]}
    assert rebuilt == frozen
    for item in exported["context"]:
        assert sha256(item["text"].encode("utf-8")).hexdigest() == item["sha256"]
    # The digest binds the text: an edited snapshot is a different frozen input.
    before = rc.input_digest(raw_case)
    raw_case["context"]["items"][0]["text"] += " (edited)"
    assert rc.input_digest(raw_case) != before
    # Nothing outside the allow-list (labels, curator refs, ground truth) leaks.
    for item in exported["context"]:
        assert set(item) == set(rc.REVIEWER_CONTEXT_FIELDS)
    blob = json.dumps(exported)
    for withheld in raw_case["withheld"]:
        assert withheld not in blob
    for defect in raw_case["defects"].values():
        assert defect["summary"] not in blob


@pytest.mark.parametrize("field", ["text", "base_ref", "env_file", "validation", "id"])
def test_reviewer_visible_withheld_sha_is_refused(syn_corpus, field):
    c = deepcopy(syn_corpus)
    case = c["cases"]["alpha-defect"]
    withheld_full = case["withheld"][0]
    withheld_short = withheld_full[:7]
    if field == "text":
        txt = f"See commit {withheld_short} for the fix."
        case["context"]["items"][0]["text"] = txt
        case["context"]["items"][0]["sha256"] = sha256(txt.encode()).hexdigest()
    elif field == "base_ref":
        c["repository"]["refs"][withheld_full] = c["repository"]["refs"].pop("main")
        case["review"]["base_ref"] = withheld_full
    elif field == "env_file":
        bad_path = f"dir/{withheld_short}.toml"
        c["repository"]["environment"]["files"].append(bad_path)
        for oc in c["cases"].values():
            oc["environment"]["files"][bad_path] = None
    elif field == "validation":
        c["repository"]["environment"]["validation"] = [f"echo {withheld_short}"]
        for oc in c["cases"].values():
            oc["environment"]["validation"] = [f"echo {withheld_short}"]
    else:
        case["context"]["items"][0]["id"] = f"item-{withheld_short}"
    with pytest.raises(rc.CorpusError, match="withheld commit"):
        rc.check_corpus(c)


def test_post_cutoff_refs_and_timestamps_and_inventory_checks(syn_corpus):
    leaked_ref = deepcopy(syn_corpus)
    leaked_ref["cases"]["alpha-defect"]["post_cutoff_refs"] = ["PR #999"]
    txt = "Fixed later in pr #999."
    leaked_ref["cases"]["alpha-defect"]["context"]["items"][0]["text"] = txt
    leaked_ref["cases"]["alpha-defect"]["context"]["items"][0]["sha256"] = sha256(
        txt.encode()
    ).hexdigest()
    with pytest.raises(rc.CorpusError, match="post-cutoff reference"):
        rc.check_corpus(leaked_ref)

    late_obs = deepcopy(syn_corpus)
    late_obs["cases"]["alpha-defect"]["context"]["items"][0]["observed_at"] = "2026-05-01T12:00:01Z"
    with pytest.raises(rc.CorpusError, match="observed after the cutoff"):
        rc.check_corpus(late_obs)

    late_edit = deepcopy(syn_corpus)
    late_edit["cases"]["alpha-defect"]["context"]["items"][0]["edited_at"] = "2026-05-01T12:00:01Z"
    with pytest.raises(rc.CorpusError, match="edited after the cutoff"):
        rc.check_corpus(late_edit)

    bad_digest = deepcopy(syn_corpus)
    bad_digest["cases"]["alpha-defect"]["context"]["items"][0]["sha256"] = "0" * 64
    with pytest.raises(rc.CorpusError, match="context digest"):
        rc.check_corpus(bad_digest)

    missing_issue = deepcopy(syn_corpus)
    missing_issue["cases"]["alpha-defect"]["context"]["items"].pop(0)
    missing_issue["cases"]["alpha-defect"]["context"]["inventory"]["sources"].remove("issue/1")
    with pytest.raises(rc.CorpusError, match="holds the case's issue"):
        rc.check_corpus(missing_issue)

    mismatch_inv = deepcopy(syn_corpus)
    mismatch_inv["cases"]["alpha-defect"]["context"]["inventory"]["sources"].append(
        "document/extra"
    )
    with pytest.raises(rc.CorpusError, match="inventory lists exactly"):
        rc.check_corpus(mismatch_inv)


def test_cross_case_future_round_sha_leakage_refused(tmp_path):
    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    # Add round 2 of PR #2 at a later cutoff (pr2_fix is round 2's head!),
    # even if round 1 omitted pr2_fix from withheld.
    c["cases"]["pr2-r1"]["withheld"] = []
    c["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["discovery"]["fix"] = None
    c["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["discovery"]["evidence_status"] = "provisional"
    txt = f"Round 2 head is {h['pr2_fix'][:7]}"
    c["cases"]["pr2-r1"]["context"]["items"][0]["text"] = txt
    c["cases"]["pr2-r1"]["context"]["items"][0]["sha256"] = sha256(txt.encode()).hexdigest()

    r2 = deepcopy(c["cases"]["pr2-r1"])
    r2["provenance"]["round"] = 2
    r2["review"]["head"] = h["pr2_fix"]
    r2["review"]["head_tree"] = h["tree"](h["pr2_fix"])
    r2["review"]["cutoff"] = "2026-01-04T02:00:00Z"
    r2["environment"]["files"] = {
        "pyproject.toml": h["blob"](h["pr2_fix"], "pyproject.toml"),
        "uv.lock": h["blob"](h["pr2_fix"], "uv.lock"),
    }
    r2["context"]["inventory"]["taken"] = "2026-01-04T03:00:00Z"
    r2["context"]["items"] = [
        _item(
            "issue", "issue", "issue/1", "Fix line2 cleanly.", observed_at="2026-01-02T12:00:00Z"
        ),
        _item("pr-body", "pr_body", "pr/2", "Updates line2.", observed_at="2026-01-03T01:30:00Z"),
    ]
    r2["defects"] = {
        "d-pr2-2": {
            "severity": "low",
            "class": "specification",
            "introduced": h["pr2_fix"],
            "summary": "doc nit",
            "location": [{"path": "src/a.py", "lines": [2, 2]}],
            "discovery": {
                "source": "same_review",
                "round": 2,
                "finding": "R2-F1",
                "discovered": "2026-01-05",
                "fix": None,
                "merged": None,
                "evidence_status": "recorded",
                "artifact": None,
            },
            "labels": [_label("adj1", "confirmed", decided="2026-01-10")],
        }
    }
    c["cases"]["pr2-r2"] = r2
    with pytest.raises(rc.CorpusError, match="withheld commit"):
        rc.check_corpus(c)


def test_cross_case_leakage_refuses_a_later_intermediate_introduction(tmp_path):
    """A later round of two commits, B then its head C, registers a defect B
    introduced. Round 1's text naming B is refused although B is neither round
    2's head nor withheld anywhere; naming a later introduction round 1's own
    candidate holds (its head) is not."""
    h = _make_hist_repo(tmp_path)
    repo = h["repo"]
    _git(repo, "checkout", "-q", "-b", "pr2-round2", h["pr2_head"])
    text = (repo / "src/a.py").read_text(encoding="utf-8")
    (repo / "src/a.py").write_text(text.replace("line3 = 3", "line3 = 0  # bug"), encoding="utf-8")
    _git(repo, "commit", "-qam", "feat: round 2 part 1", dt="2026-01-04T03:00:00Z")
    intermediate = _git(repo, "rev-parse", "HEAD")
    (repo / "notes.txt").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feat: round 2 part 2", dt="2026-01-04T04:00:00Z")
    round2_head = _git(repo, "rev-parse", "HEAD")
    assert intermediate != round2_head

    c = _hist_branch_corpus(h)
    r2 = deepcopy(c["cases"]["pr2-r1"])
    r2["provenance"]["round"] = 2
    r2["review"]["head"] = round2_head
    r2["review"]["head_tree"] = h["tree"](round2_head)
    r2["review"]["cutoff"] = "2026-01-04T05:00:00Z"
    r2["environment"]["files"] = {
        "pyproject.toml": h["blob"](round2_head, "pyproject.toml"),
        "uv.lock": h["blob"](round2_head, "uv.lock"),
    }
    r2["context"]["inventory"]["taken"] = "2026-01-04T06:00:00Z"
    r2["withheld"] = []

    def round2_defect(introduced: str, line: int, finding: str) -> dict:
        return {
            "severity": "low",
            "class": "correctness",
            "introduced": introduced,
            "summary": f"wrong line{line}",
            "location": [{"path": "src/a.py", "lines": [line, line]}],
            "discovery": {
                "source": "same_review",
                "round": 2,
                "finding": finding,
                "discovered": "2026-01-05",
                "fix": None,
                "merged": None,
                "evidence_status": "provisional",
                "artifact": None,
            },
            "labels": [_label("adj1", "confirmed", decided="2026-01-10")],
        }

    r2["defects"] = {
        "d-pr2-early": round2_defect(h["pr2_head"], 2, "R2-F1"),
        "d-pr2-late": round2_defect(intermediate, 3, "R2-F2"),
    }
    c["cases"]["pr2-r2"] = r2

    def round1_names(commit: str) -> dict:
        named = deepcopy(c)
        item = named["cases"]["pr2-r1"]["context"]["items"][0]
        item["text"] = f"See {commit[:7]}."
        item["sha256"] = sha256(item["text"].encode()).hexdigest()
        return named

    # Both rounds are valid and verify, so only the leakage rule tells them apart.
    present = round1_names(h["pr2_head"])
    rc.check_corpus(present)
    report = rc.verify(present, repo)
    assert report["ok"], report

    with pytest.raises(rc.CorpusError, match="withheld commit"):
        rc.check_corpus(round1_names(intermediate))


# -- C. Batched read-only Git verification ------------------------------------


def _merge_pr_case(
    h: dict,
    *,
    pr: int,
    issue: int,
    base_tip: str,
    merge_base: str,
    head: str,
    merge_commit: str,
    cutoff: str,
    opened: str,
    defect: tuple[str, str, str, str, str],
    decided: str,
    withheld: list[str],
) -> dict:
    """A merge_pr case whose one defect, at line 2 of ``path``, a later commit fixes."""
    key, path, source, discovered, fix = defect
    tree, blob = h["tree"], h["blob"]
    return {
        "stratum": "revert" if source == "revert" else "escape",
        "cluster": f"cl-pr{pr}",
        "provenance": {
            "source": "merge_pr",
            "issue": issue,
            "pr": pr,
            "round": None,
            "base_evidence": "merge_parent",
            "merge_commit": merge_commit,
            "selection": "test",
            "note": "",
        },
        "review": {
            "base_ref": "main",
            "base_tip": base_tip,
            "merge_base": merge_base,
            "head": head,
            "base_tree": tree(base_tip),
            "merge_base_tree": tree(merge_base),
            "head_tree": tree(head),
            "cutoff": cutoff,
        },
        "environment": {
            "files": {
                "pyproject.toml": blob(head, "pyproject.toml"),
                "uv.lock": blob(head, "uv.lock"),
            },
            "validation_scope": "full",
            "validation": ["make check"],
        },
        "context": {
            "inventory": {
                "status": "complete",
                "taken": cutoff[:11] + "01:00:00Z",
                "artifact": "inv",
                "edit_history_verified": True,
                "sources": [f"issue/{issue}", f"pr/{pr}"],
            },
            "items": [
                _item(
                    "issue",
                    "issue",
                    f"issue/{issue}",
                    f"Add {path}.",
                    observed_at=f"{opened}T00:00:00Z",
                ),
                _item(
                    "pr-body", "pr_body", f"pr/{pr}", f"PR {pr}.", observed_at=f"{opened}T02:00:00Z"
                ),
            ],
        },
        "actors": ["producer", "reviewer", "fixer"],
        "risk": {"stratum": "routine", "rationale": "test"},
        "exposure": {"possible_from": opened, "public_by": None, "artifact": "git"},
        "defects": {
            key: {
                "severity": "high",
                "class": "reliability",
                "introduced": head,
                "summary": f"bad {path}",
                "location": [{"path": path, "lines": [2, 2]}],
                "discovery": {
                    "source": source,
                    "round": None,
                    "finding": None,
                    "discovered": discovered,
                    "fix": fix,
                    "merged": None,
                    "evidence_status": "recorded",
                    "artifact": f"{source} commit",
                },
                "labels": [_label("adj1", "confirmed", decided=decided)],
            }
        },
        "negative": None,
        "withheld": withheld,
        "post_cutoff_refs": [],
    }


def test_verify_all_three_topologies_and_post_merge_lineage(tmp_path):
    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    tree, blob = h["tree"], h["blob"]

    # Add squash case (PR #2) with escape defect fixed at post_fix2
    c["cases"]["pr2-squash"] = {
        "stratum": "escape",
        "cluster": "cl-pr2",
        "provenance": {
            "source": "squash",
            "issue": 1,
            "pr": 2,
            "round": None,
            "base_evidence": "squash_parent",
            "merge_commit": h["squash2"],
            "selection": "test",
            "note": "",
        },
        "review": {
            "base_ref": "main",
            "base_tip": h["base1"],
            "merge_base": h["base1"],
            "head": h["squash2"],
            "base_tree": tree(h["base1"]),
            "merge_base_tree": tree(h["base1"]),
            "head_tree": tree(h["squash2"]),
            "cutoff": "2026-01-06T00:00:00Z",
        },
        "environment": {
            "files": {
                "pyproject.toml": blob(h["squash2"], "pyproject.toml"),
                "uv.lock": blob(h["squash2"], "uv.lock"),
            },
            "validation_scope": "full",
            "validation": ["make check"],
        },
        "context": {
            "inventory": {
                "status": "complete",
                "taken": "2026-01-06T01:00:00Z",
                "artifact": "inv",
                "edit_history_verified": True,
                "sources": ["issue/1", "pr/2"],
            },
            "items": [
                _item(
                    "issue", "issue", "issue/1", "Fix line2.", observed_at="2026-01-02T12:00:00Z"
                ),
                _item(
                    "pr-body",
                    "pr_body",
                    "pr/2",
                    "Updates line2.",
                    observed_at="2026-01-03T01:30:00Z",
                ),
            ],
        },
        "actors": ["producer", "reviewer", "fixer"],
        "risk": {"stratum": "routine", "rationale": "test"},
        "exposure": {"possible_from": "2026-01-06", "public_by": None, "artifact": "git"},
        "defects": {
            "d-sq-1": {
                "severity": "medium",
                "class": "correctness",
                "introduced": h["squash2"],
                "summary": "escaped bug on line 2",
                "location": [{"path": "src/a.py", "lines": [2, 2]}],
                "discovery": {
                    "source": "post_merge",
                    "round": None,
                    "finding": None,
                    "discovered": "2026-01-08",
                    "fix": h["post_fix2"],
                    "merged": None,
                    "evidence_status": "recorded",
                    "artifact": "post-merge fix",
                },
                "labels": [_label("adj1", "confirmed", decided="2026-01-10")],
            }
        },
        "negative": None,
        "withheld": [h["post_fix2"]],
        "post_cutoff_refs": [],
    }

    # Add 2-parent merge_pr case (PR #3) with revert defect fixed at revert3
    c["cases"]["pr3-merge"] = _merge_pr_case(
        h,
        pr=3,
        issue=10,
        base_tip=h["pr3_base"],
        merge_base=h["post_fix2"],
        head=h["pr3_head"],
        merge_commit=h["merge3"],
        cutoff="2026-01-11T00:00:00Z",
        opened="2026-01-10",
        defect=("d-m3-1", "src/b.py", "revert", "2026-01-12", h["revert3"]),
        decided="2026-01-15",
        withheld=[h["revert3"]],
    )

    res = rc.verify(c, h["repo"])
    assert res["ok"] is True
    assert all(v["ok"] for v in res["cases"].values())
    assert rc.score(c, _subs_for(c), h["repo"])["verified"]["ok"] is True


def test_verify_refuses_a_merge_pr_defect_its_merge_resolved_away(tmp_path):
    """A conflict resolution that drops the defect leaves nothing that landed with the PR.

    PR #4's head adds ``bad = 2``; the base adds ``good = 2`` at the same place,
    and the merge resolves the conflict to the base's side. A later commit
    reintroduces ``bad = 2`` and a still-later one fixes it, so the fix
    changes the head's lines; only the merge's own tree shows the defect never
    landed with the PR.
    """
    h = _make_hist_repo(tmp_path)
    repo = h["repo"]
    fork = h["revert3"]
    _git(repo, "checkout", "-b", "pr4", fork)
    (repo / "src/c.py").write_text("ok = 1\nbad = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: pr4 head", dt="2026-01-13T01:00:00Z")
    pr4_head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "main")
    (repo / "src/c.py").write_text("ok = 1\ngood = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: c.py on main", dt="2026-01-13T02:00:00Z")
    pr4_base = _git(repo, "rev-parse", "HEAD")

    _git(
        repo,
        "merge",
        "--no-ff",
        "-X",
        "ours",
        "-m",
        "Merge pull request #4",
        "pr4",
        dt="2026-01-14T00:00:00Z",
    )
    merge4 = _git(repo, "rev-parse", "HEAD")
    assert (repo / "src/c.py").read_text(encoding="utf-8") == "ok = 1\ngood = 2\n"

    (repo / "src/c.py").write_text("ok = 1\nbad = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "reintroduce bad", dt="2026-01-15T00:00:00Z")
    reintroduced = _git(repo, "rev-parse", "HEAD")
    (repo / "src/c.py").write_text("ok = 1\nfixed = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "fix: bad", dt="2026-01-16T00:00:00Z")
    fix4 = _git(repo, "rev-parse", "HEAD")

    c = _hist_branch_corpus(h)
    c["repository"]["refs"]["main"] = fix4
    c["cases"]["pr4-merge"] = _merge_pr_case(
        h,
        pr=4,
        issue=40,
        base_tip=pr4_base,
        merge_base=fork,
        head=pr4_head,
        merge_commit=merge4,
        cutoff="2026-01-14T00:00:00Z",
        opened="2026-01-13",
        defect=("d-m4-1", "src/c.py", "post_merge", "2026-01-16", fix4),
        decided="2026-01-20",
        withheld=[reintroduced, fix4],
    )
    rc.check_corpus(c)

    res = rc.verify(c, h["repo"])
    assert res["ok"] is False
    assert res["cases"]["pr4-merge"]["failed"] == ["defect d-m4-1 survives its merge"]
    assert res["cases"]["pr2-r1"]["ok"] is True

    # A whole-file location names no block whose survival the merge tree could show.
    whole = deepcopy(c)
    whole["cases"]["pr4-merge"]["defects"]["d-m4-1"]["location"][0]["lines"] = None
    with pytest.raises(rc.CorpusError, match="a merge_pr defect is located by its lines"):
        rc.check_corpus(whole)


def test_verify_refuses_tampered_git_pins_and_bad_lineage(tmp_path):
    h = _make_hist_repo(tmp_path)

    # Wrong merge_base (older ancestor `root` instead of `base1`)
    wrong_mb = _hist_branch_corpus(h)
    wrong_mb["cases"]["pr2-r1"]["review"]["merge_base"] = h["root"]
    wrong_mb["cases"]["pr2-r1"]["review"]["merge_base_tree"] = h["tree"](h["root"])
    res = rc.verify(wrong_mb, h["repo"])
    assert res["ok"] is False
    assert "merge base" in " ".join(res["cases"]["pr2-r1"]["failed"])

    # Tampered head_tree
    bad_tree = _hist_branch_corpus(h)
    bad_tree["cases"]["pr2-r1"]["review"]["head_tree"] = h["tree"](h["base1"])
    assert "head tree" in rc.verify(bad_tree, h["repo"])["cases"]["pr2-r1"]["failed"]

    # Ordinary 2-parent merge disguised as squash
    bad_sq = _hist_branch_corpus(h)
    sq_case = bad_sq["cases"]["pr2-r1"]
    sq_case["provenance"].update(
        source="squash", round=None, base_evidence="squash_parent", pr=3, merge_commit=h["merge3"]
    )
    sq_case["review"].update(
        base_tip=h["post_fix2"],
        merge_base=h["post_fix2"],
        head=h["merge3"],
        head_tree=h["tree"](h["merge3"]),
        cutoff="2026-01-11T00:00:00Z",
    )
    sq_case["review"]["base_tree"] = h["tree"](h["post_fix2"])
    sq_case["review"]["merge_base_tree"] = h["tree"](h["post_fix2"])
    sq_case["environment"]["files"] = {
        "pyproject.toml": h["blob"](h["merge3"], "pyproject.toml"),
        "uv.lock": h["blob"](h["merge3"], "uv.lock"),
    }
    sq_case["context"]["inventory"]["sources"] = ["issue/1", "pr/3"]
    sq_case["context"]["inventory"]["taken"] = "2026-01-12T00:00:00Z"
    sq_case["context"]["items"][1]["source"] = "pr/3"
    sq_case["defects"]["d-pr2-1"]["introduced"] = h["merge3"]
    sq_case["defects"]["d-pr2-1"]["discovery"].update(
        source="revert", round=None, finding=None, discovered="2026-01-12", fix=h["revert3"]
    )
    sq_case["defects"]["d-pr2-1"]["labels"][0]["decided"] = "2026-01-15"
    sq_case["stratum"] = "revert"
    sq_case["withheld"] = [h["revert3"]]
    assert (
        "a squash candidate has one parent"
        in rc.verify(bad_sq, h["repo"])["cases"]["pr2-r1"]["failed"]
    )

    # Fix touching wrong lines fails `addresses`
    bad_fix_lines = _hist_branch_corpus(h)
    bad_fix_lines["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["location"] = [
        {"path": "src/a.py", "lines": [4, 4]}
    ]
    assert "fix changes what it is located in" in " ".join(
        rc.verify(bad_fix_lines, h["repo"])["cases"]["pr2-r1"]["failed"]
    )

    # Pre-merge discovery date for post_merge defect on pr_branch with merged squash
    pre_merge = _hist_branch_corpus(h)
    pre_merge["cases"]["pr2-r1"]["stratum"] = "escape"
    pre_merge["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["discovery"].update(
        source="post_merge",
        round=None,
        finding=None,
        discovered="2026-01-05",
        fix=h["post_fix2"],
        merged=h["squash2"],
    )
    pre_merge["cases"]["pr2-r1"]["withheld"] = [h["post_fix2"], h["squash2"]]
    assert "found no earlier than its merge" in " ".join(
        rc.verify(pre_merge, h["repo"])["cases"]["pr2-r1"]["failed"]
    )


def test_verify_is_strictly_read_only_and_ignores_hostile_git_env(tmp_path, monkeypatch):
    h = _make_hist_repo(tmp_path)
    repo: Path = h["repo"]
    corpus = _hist_branch_corpus(h)
    # Plant a hostile local git config hook and replacement ref!
    _git(repo, "config", "core.hooksPath", str(tmp_path / "evil-hooks"))
    _git(repo, "replace", h["pr2_head"], h["root"])
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent-git-dir"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor=echo pwned'")

    def snap_git_dir(root: Path) -> list[tuple[str, bytes]]:
        return sorted(
            (p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file()
        )

    before = snap_git_dir(repo / ".git")
    res = rc.verify(corpus, repo)
    after = snap_git_dir(repo / ".git")
    assert res["ok"] is True
    assert before == after


# -- D. Cross-case corpus consistency -----------------------------------------


def test_cross_case_consistency_refuses_duplicates_and_contradictions(syn_corpus):
    # Duplicate (issue, round) when pr is None (NEW-7a)
    dup_issue_round = deepcopy(syn_corpus)
    c2 = deepcopy(dup_issue_round["cases"]["alpha-defect"])
    c2["review"]["head"] = "9" * 40
    c2["context"]["items"][0]["text"] = "Slightly different text."
    c2["context"]["items"][0]["sha256"] = sha256(b"Slightly different text.").hexdigest()
    dup_issue_round["cases"]["alpha-defect-copy"] = c2
    with pytest.raises(rc.CorpusError, match="one review round of an issue"):
        rc.check_corpus(dup_issue_round)

    # Same defect_id confirmed at round 1 and rejected at round 2 (NEW-7b)
    contradict = deepcopy(syn_corpus)
    r2 = deepcopy(contradict["cases"]["alpha-defect"])
    r2["provenance"]["round"] = 2
    r2["review"]["head"] = "9" * 40
    r2["defects"] = {"alpha-d2": deepcopy(r2["defects"]["alpha-d2"])}
    r2["defects"]["alpha-d2"]["discovery"]["source"] = "same_review"
    r2["defects"]["alpha-d2"]["labels"] = [_label("adj-other", "rejected", decided="2026-06-25")]
    contradict["cases"]["alpha-defect-r2"] = r2
    with pytest.raises(rc.CorpusError, match="one defect"):
        rc.check_corpus(contradict)

    # Cluster split across cases of the same issue
    split_cluster = deepcopy(syn_corpus)
    split_cluster["cases"]["alpha-negative"]["cluster"] = "other-cluster"
    with pytest.raises(rc.CorpusError, match="share a cluster"):
        rc.check_corpus(split_cluster)

    # Later-round discovery dated before credited round's cutoff
    early_disc = deepcopy(syn_corpus)
    r2_early = deepcopy(early_disc["cases"]["alpha-defect"])
    r2_early["provenance"]["round"] = 2
    r2_early["review"]["head"] = "9" * 40
    r2_early["review"]["cutoff"] = "2026-05-05T00:00:00Z"
    r2_early["context"]["items"][0]["text"] = "r2 text"
    r2_early["context"]["items"][0]["sha256"] = sha256(b"r2 text").hexdigest()
    r2_early["defects"] = {"alpha-d4": deepcopy(r2_early["defects"]["alpha-d1"])}
    r2_early["defects"]["alpha-d4"]["discovery"]["source"] = "same_review"
    r2_early["defects"]["alpha-d4"]["discovery"]["round"] = 2
    r2_early["defects"]["alpha-d4"]["discovery"]["finding"] = "R2-F9"
    r2_early["defects"]["alpha-d4"]["discovery"]["discovered"] = "2026-05-05"
    r2_early["defects"]["alpha-d4"]["discovery"]["fix"] = None
    r2_early["defects"]["alpha-d4"]["labels"] = [_label("adj-1", "confirmed", decided="2026-06-21")]
    # alpha-defect's alpha-d2 credits round 2 on 2026-05-03, before round 2's cutoff 2026-05-05!
    early_disc["cases"]["alpha-defect-r2"] = r2_early
    with pytest.raises(rc.CorpusError, match="no earlier than its round"):
        rc.check_corpus(early_disc)


def test_duplicate_json_key_and_one_pr_one_merge(tmp_path):
    raw = b'{"a": 1, "a": 2}'
    p = tmp_path / "dup.json"
    p.write_bytes(raw)
    with pytest.raises(rc.CorpusError, match="duplicate JSON key"):
        rc.read_json(p)

    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    # Branch defect names merged=squash2 while a squash case for PR #2 names post_fix2
    c["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["discovery"]["merged"] = h["squash2"]
    c["cases"]["pr2-r1"]["defects"]["d-pr2-1"]["discovery"]["fix"] = h["post_fix2"]
    c["cases"]["pr2-r1"]["withheld"] = [h["pr2_fix"], h["squash2"], h["post_fix2"]]
    sq = deepcopy(c["cases"]["pr2-r1"])
    sq["provenance"].update(
        source="squash", round=None, base_evidence="squash_parent", merge_commit=h["post_fix2"]
    )
    sq["review"]["head"] = h["post_fix2"]
    sq["defects"] = {}
    sq["stratum"] = "hard_negative"
    sq["negative"] = {"observed_through": "2026-02-01", "oracle_status": "pass", "labels": []}
    sq["withheld"] = []
    c["cases"]["pr2-sq"] = sq
    with pytest.raises(rc.CorpusError, match="one PR merged as one commit"):
        rc.check_corpus(c)


# -- E. Adjudication, independence & exposure ---------------------------------


def test_adjudication_independence_and_arbiter_constraints(syn_corpus, syn_subs):
    # Variant identity cannot adjudicate any corpus defect or negative
    var_label = deepcopy(syn_corpus)
    var_label["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
        _label("reviewer-baseline", "confirmed", decided="2026-06-21")
    )
    checked_var = rc.check_corpus(var_label)
    subs_var = deepcopy(syn_subs)
    subs_var["corpus"] = checked_var.digest
    with pytest.raises(rc.CorpusError, match="variant cannot label the corpus"):
        rc.check_submissions(checked_var, subs_var)

    # Critical defect needs 2 agreeing adjudicators even with arbiter
    crit_arb = [
        _label("adj1", "confirmed"),
        _label("adj2", "rejected"),
        _label("arb1", "confirmed", role="arbiter"),
    ]
    assert rc.settle(crit_arb, rc.critical_needs_two("critical")) is None
    crit_arb.append(_label("adj3", "confirmed"))
    assert rc.settle(crit_arb, rc.critical_needs_two("critical")) == "confirmed"

    # Arbiter CANNOT invent an unvoted 3rd verdict on non-critical disagreement (NEW-4)
    unvoted_arb = [
        _label("adj1", "match", defect="alpha-d1", with_defect=True),
        _label("adj2", "match", defect="alpha-d2", with_defect=True),
        _label("arb1", "false", defect=None, role="arbiter", with_defect=True),
    ]
    assert rc.settle(unvoted_arb) is None
    # Voting for one of the two adjudicator options does settle
    unvoted_arb[2]["verdict"] = "match"
    unvoted_arb[2]["defect"] = "alpha-d1"
    assert rc.settle(unvoted_arb) == ("match", "alpha-d1")


def test_an_actor_of_any_case_cannot_adjudicate_another(syn_corpus, syn_subs):
    # An actor of another thread's case votes on alpha-defect's defect
    other_thread = deepcopy(syn_corpus)
    other_thread["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
        _label("reviewer-beta", "confirmed", decided="2026-06-21")
    )
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_corpus(other_thread)

    # A later round of the same thread: its own actor settles an earlier round's defect
    later_round = deepcopy(syn_corpus)
    later_round["cases"]["alpha-negative"]["actors"].append("late-fixer")
    later_round["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
        _label("late-fixer", "confirmed", decided="2026-06-21")
    )
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_corpus(later_round)

    # An actor of another case cannot match a run's finding either
    subs = deepcopy(syn_subs)
    subs["runs"][0]["findings"][0]["labels"][0]["by"] = "reviewer-gamma"
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_submissions(rc.check_corpus(syn_corpus), subs)


def test_identities_are_canonicalized_case_insensitively(syn_corpus, syn_subs):
    # Same-case actor mixed-case alias on a defect label and a negative label
    same_defect = deepcopy(syn_corpus)
    same_defect["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
        _label("Producer-Alpha", "confirmed", decided="2026-06-21")
    )
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_corpus(same_defect)

    same_negative = deepcopy(syn_corpus)
    same_negative["cases"]["alpha-negative"]["negative"]["labels"].append(
        _label("PRODUCER-ALPHA", "clean", decided="2026-06-21")
    )
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_corpus(same_negative)

    # Cross-case actor mixed-case alias on a defect label and a finding-match label
    cross_defect = deepcopy(syn_corpus)
    cross_defect["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
        _label("Reviewer-Beta", "confirmed", decided="2026-06-21")
    )
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_corpus(cross_defect)

    cross_finding = deepcopy(syn_subs)
    cross_finding["runs"][0]["findings"][0]["labels"][0]["by"] = "Reviewer-Gamma"
    with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
        rc.check_submissions(rc.check_corpus(syn_corpus), cross_finding)

    # Duplicate actors differing only by case
    dup_actor = deepcopy(syn_corpus)
    dup_actor["cases"]["alpha-defect"]["actors"].append("Producer-Alpha")
    with pytest.raises(rc.CorpusError, match="historical actor repeated"):
        rc.check_corpus(dup_actor)

    # Two critical votes from the same account with different casing (Alice / alice)
    dup_critical_defect = deepcopy(syn_corpus)
    dup_critical_defect["cases"]["alpha-defect"]["defects"]["alpha-d2"]["labels"] = [
        _label("Alice", "confirmed", decided="2026-06-21"),
        _label("alice", "confirmed", decided="2026-06-21"),
    ]
    with pytest.raises(rc.CorpusError, match="one label per identity"):
        rc.check_corpus(dup_critical_defect)

    dup_critical_match = deepcopy(syn_subs)
    dup_critical_match["runs"][8]["findings"][0]["labels"] = [
        _label("Alice", "match", defect="beta-d1", decided="2026-07-10", with_defect=True),
        _label("alice", "match", defect="beta-d1", decided="2026-07-10", with_defect=True),
    ]
    with pytest.raises(rc.CorpusError, match="one label per identity"):
        rc.check_submissions(rc.check_corpus(syn_corpus), dup_critical_match)

    with pytest.raises(rc.CorpusError, match="one label per identity"):
        rc.settle(
            [_label("Alice", "confirmed"), _label("alice", "confirmed")],
            rc.critical_needs_two("critical"),
        )

    # Variant identity and variant name mixed-case aliases on corpus and finding labels
    for alias in ("Reviewer-Baseline", "Baseline"):
        var_corpus = deepcopy(syn_corpus)
        var_corpus["cases"]["alpha-defect"]["defects"]["alpha-d1"]["labels"].append(
            _label(alias, "confirmed", decided="2026-06-21")
        )
        checked_var = rc.check_corpus(var_corpus)
        subs_var = deepcopy(syn_subs)
        subs_var["corpus"] = checked_var.digest
        with pytest.raises(rc.CorpusError, match="variant cannot label the corpus"):
            rc.check_submissions(checked_var, subs_var)

        var_finding = deepcopy(syn_subs)
        var_finding["runs"][0]["findings"][0]["labels"][0]["by"] = alias
        with pytest.raises(rc.CorpusError, match="an actor cannot adjudicate"):
            rc.check_submissions(rc.check_corpus(syn_corpus), var_finding)

    # Two variants with identities differing only by case
    dup_var = deepcopy(syn_subs)
    dup_var["variants"]["baseline"]["identity"] = "Reviewer-Alpha"
    dup_var["variants"]["variant-a"]["identity"] = "reviewer-alpha"
    with pytest.raises(rc.CorpusError, match="variant identity repeated"):
        rc.check_submissions(rc.check_corpus(syn_corpus), dup_var)


def test_rejected_defect_excluded_from_exposure_and_refuted_negative_unrunnable(syn_corpus):
    # NEW-8a: gamma-revert has gamma-d2 (rejected, 2026-04-02) and gamma-d1 (confirmed, 2026-04-10).
    # Moving gamma-d1/d3 to 2026-04-20 means rejected gamma-d2 must NOT pull Case.public earlier.
    c = deepcopy(syn_corpus)
    g = c["cases"]["gamma-revert"]
    g["defects"]["gamma-d1"]["discovery"]["discovered"] = "2026-04-20"
    g["defects"]["gamma-d3"]["discovery"]["discovered"] = "2026-04-20"
    g["defects"]["gamma-d2"]["discovery"]["discovered"] = "2026-04-02"
    g["exposure"]["possible_from"] = "2026-04-01"
    checked = rc.check_corpus(c)
    assert str(checked.cases["gamma-revert"].public) == "2026-04-20"
    assert rc.exposure(checked.cases["gamma-revert"], "2026-04-05") == "unknown"

    # NEW-8b: Refuted negative is unrunnable; oracle_status != "pass" is never settled
    c2 = deepcopy(syn_corpus)
    c2["cases"]["beta-clean"]["negative"]["labels"] = [
        _label("adj1", "defective", decided="2026-06-25")
    ]
    c2["cases"]["alpha-negative"]["negative"]["oracle_status"] = "not_run"
    chk2 = rc.check_corpus(c2)
    assert chk2.cases["beta-clean"].negative == "refuted"
    assert chk2.cases["beta-clean"].runnable is False
    assert chk2.cases["alpha-negative"].negative == "unverified_oracle"


# -- F. Scoring, Option 3A recall bounds, bootstrap & censoring ---------------


def test_synthetic_scorecard_is_deterministic_and_matches_hand_values(syn_corpus, syn_subs):
    r1 = rc.score(syn_corpus, syn_subs)
    r2 = rc.score(syn_corpus, syn_subs)
    assert r1 == r2
    assert r1["claim"] == rc.CLAIM

    base = r1["variants"]["baseline"]
    alt = r1["variants"]["variant-a"]
    assert base["findings"] == {"confirmed": 4, "false": 4, "unknown": 2, "duplicates": 1}
    assert base["safety"] == {"missed_critical_defects": 2, "clean_verdict_critical_escapes": 0}
    assert base["training_exposure"] == {"none": 4, "possible": 2, "unknown": 0}
    assert base["metrics"]["recall"]["point"] == "7/23"
    assert base["metrics"]["recall"]["bounds"] == ["7/33", "25/33"]
    assert base["metrics"]["false_positive_rate"]["point"] == "1/2"

    assert alt["findings"] == {"confirmed": 3, "false": 1, "unknown": 2, "duplicates": 0}
    assert alt["safety"] == {"missed_critical_defects": 0, "clean_verdict_critical_escapes": 0}
    assert alt["training_exposure"] == {"none": 0, "possible": 0, "unknown": 6}
    assert alt["cost_microusd"] is None
    assert alt["metrics"]["recall"]["point"] == "20/23"
    assert alt["metrics"]["recall"]["bounds"] == ["20/33", "21/23"]
    assert alt["metrics"]["false_positive_rate"]["bounds"] == ["0", "1/2"]


def test_option_3a_unsettled_critical_defect_widens_recall_bounds_and_disables_usable(tmp_path):
    """Confirmed low + unsettled critical missed -> bounds [1/10, 1/2], usable=False."""
    h = _make_hist_repo(tmp_path)
    c = _hist_branch_corpus(h)
    c["cases"]["pr2-r1"]["defects"]["d-pr2-crit"] = {
        "severity": "critical",
        "class": "security",
        "introduced": h["pr2_head"],
        "summary": "unsettled critical",
        "location": [{"path": "src/a.py", "lines": [1, 1]}],
        "discovery": {
            "source": "same_review",
            "round": 1,
            "finding": "R1-F2",
            "discovered": "2026-01-04",
            "fix": None,
            "merged": None,
            "evidence_status": "recorded",
            "artifact": None,
        },
        "labels": [
            _label("adj1", "confirmed", decided="2026-01-10")
        ],  # only 1 adjudicator -> unknown
    }
    # Add a 2nd defect cluster (PR #3) so clusters == 2 and usable=False from label_bands_within.
    c["frozen"] = "2026-02-15"
    c["cases"]["pr3-merge"] = {
        "stratum": "revert",
        "cluster": "cl-2",
        "provenance": {
            "source": "merge_pr",
            "issue": 99,
            "pr": 3,
            "round": None,
            "base_evidence": "merge_parent",
            "merge_commit": h["merge3"],
            "selection": "test",
            "note": "",
        },
        "review": {
            "base_ref": "main",
            "base_tip": h["pr3_base"],
            "merge_base": h["post_fix2"],
            "head": h["pr3_head"],
            "base_tree": h["tree"](h["pr3_base"]),
            "merge_base_tree": h["tree"](h["post_fix2"]),
            "head_tree": h["tree"](h["pr3_head"]),
            "cutoff": "2026-01-11T00:00:00Z",
        },
        "environment": {
            "files": {
                "pyproject.toml": h["blob"](h["pr3_head"], "pyproject.toml"),
                "uv.lock": h["blob"](h["pr3_head"], "uv.lock"),
            },
            "validation_scope": "full",
            "validation": ["make check"],
        },
        "context": {
            "inventory": {
                "status": "complete",
                "taken": "2026-01-11T01:00:00Z",
                "artifact": "inv",
                "edit_history_verified": True,
                "sources": ["issue/99", "pr/3"],
            },
            "items": [
                _item("issue", "issue", "issue/99", "PR 3.", observed_at="2026-01-10T00:00:00Z"),
                _item("pr-body", "pr_body", "pr/3", "PR 3.", observed_at="2026-01-10T02:00:00Z"),
            ],
        },
        "actors": ["producer", "reviewer", "fixer"],
        "risk": {"stratum": "routine", "rationale": "test"},
        "exposure": {"possible_from": "2026-01-10", "public_by": None, "artifact": "git"},
        "defects": {
            "d-m3-low": {
                "severity": "low",
                "class": "reliability",
                "introduced": h["pr3_head"],
                "summary": "bad b.py",
                "location": [{"path": "src/b.py", "lines": [2, 2]}],
                "discovery": {
                    "source": "revert",
                    "round": None,
                    "finding": None,
                    "discovered": "2026-01-12",
                    "fix": h["revert3"],
                    "merged": None,
                    "evidence_status": "recorded",
                    "artifact": "revert commit",
                },
                "labels": [_label("adj2", "confirmed", decided="2026-01-15")],
            }
        },
        "negative": None,
        "withheld": [h["revert3"]],
        "post_cutoff_refs": [],
    }

    subs = _subs_for(c, ["pr2-r1", "pr3-merge"])
    subs["runs"][0]["findings"] = [
        {
            "id": "F1",
            "classification": "blocked",
            "title": "line2",
            "location": "src/a.py:2",
            "labels": [
                _label("adj4", "match", defect="d-pr2-1", decided="2026-01-10", with_defect=True)
            ],
        }
    ]
    report = rc.score(c, subs, h["repo"])
    rec = report["variants"]["baseline"]["metrics"]["recall"]
    assert rec["point"] == "1/2"
    assert rec["bounds"] == ["1/10", "1/2"]
    assert report["contrasts"]["variant-a"]["metrics"]["recall"]["clusters"] == 2
    assert report["contrasts"]["variant-a"]["metrics"]["recall"]["label_bands_within"] is False
    assert report["contrasts"]["variant-a"]["metrics"]["recall"]["usable"] is False


def test_cluster_bootstrap_and_excluded_runs(syn_corpus, syn_subs):
    # Excluded pair leaves contrast while preserving variant safety tallies
    subs = deepcopy(syn_subs)
    subs["runs"][8]["status"] = "excluded"
    subs["runs"][8]["exclusion"] = "infrastructure"
    subs["runs"][8]["findings"] = None
    rep = rc.score(syn_corpus, subs)
    assert rep["contrasts"]["variant-a"]["pairs"]["excluded"] == 1
    assert rep["contrasts"]["variant-a"]["pairs"]["paired"] == 5

    # Historical corpus cannot be scored without --repo verification
    seed = json.loads(rc.SEED.read_bytes())
    with pytest.raises(rc.CorpusError, match="verifies its corpus"):
        rc.score(seed, subs)


# -- G. Work, memory & file-safety bounds -------------------------------------


def test_linear_leakage_scan_and_large_seed_memory(syn_corpus, syn_subs):
    shas = [f"{i:040x}" for i in range(1, 101)]
    texts = ["clean prose " * 5000]
    rc.check_text_leakage(texts, shas, ["PR #999"] * 10)

    # Large seed integer allocates O(1) memory (never builds list of size seed)
    subs = deepcopy(syn_subs)
    big_seed = 10**18
    subs["plan"]["seeds"] = [big_seed]
    for r in subs["runs"]:
        r["seed"] = big_seed
    tracemalloc.start()
    rc.score(syn_corpus, subs)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 16 * 1024 * 1024

    # Oversized plan cap refused before expansion
    over_plan = deepcopy(syn_subs)
    over_plan["plan"]["seeds"] = list(range(1, 2000))
    with pytest.raises(rc.CorpusError, match="within its limits"):
        rc.check_submissions(rc.check_corpus(syn_corpus), over_plan)


def test_read_json_refuses_symlink_and_fifo(tmp_path):
    link = tmp_path / "link.json"
    link.symlink_to(rc.SYNTHETIC_CORPUS)
    with pytest.raises(OSError):
        rc.read_json(link)
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    with pytest.raises(rc.CorpusError, match="regular file"):
        rc.read_json(fifo)


# -- H. Seed verification, docs digests, CLI & redaction ----------------------


def _history_or_skip() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("no git history")
    seed = json.loads(rc.SEED.read_bytes())
    known = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "cat-file",
            "-e",
            seed["cases"]["pr539-round1"]["review"]["head"],
        ],
        capture_output=True,
    )
    if known.returncode != 0:
        pytest.skip("shallow clone lacks seed branch objects")


def test_seed_describes_and_verifies_locally():
    seed = rc.read_json(rc.SEED)
    desc = rc.describe(seed)
    assert desc["cases"] == 8
    assert desc["clusters"] == 3
    assert desc["runnable"] == []
    assert desc["labels"]["defects"]["unknown"] == 20
    assert desc["labels"]["negatives"]["unlabeled"] == 2

    _history_or_skip()
    ver = rc.verify(seed, REPO_ROOT)
    assert ver["ok"] is True
    assert all(case["ok"] for case in ver["cases"].values())


def test_doc_pins_all_three_fixture_digests():
    doc = (REPO_ROOT / "docs" / "review-corpus.md").read_text(encoding="utf-8")
    for path in (rc.SEED, rc.SYNTHETIC_CORPUS, rc.SYNTHETIC_SUBMISSIONS):
        assert sha256(path.read_bytes()).hexdigest() in doc


def test_cli_subcommands_and_error_redaction(tmp_path, capsys):
    assert rc.main(["describe"]) == 0
    assert json.loads(capsys.readouterr().out)["cases"] == 8

    assert rc.main(["score"]) == 0
    assert json.loads(capsys.readouterr().out)["baseline"] == "baseline"

    assert rc.main(["export-input", str(rc.SYNTHETIC_CORPUS), "alpha-defect"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert set(exported) == {"format", "version", "review", "environment", "context"}
    assert exported == rc.project_reviewer_input(
        rc.read_json(rc.SYNTHETIC_CORPUS)["cases"]["alpha-defect"]
    )

    bad = tmp_path / "secret.json"
    bad.write_text('{"SECRET_TOKEN_SHOULD_NOT_ECHO": 1}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rc.main(["describe", str(bad)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "SECRET_TOKEN_SHOULD_NOT_ECHO" not in err
    assert err == "Invalid review corpus or submissions.\n"
