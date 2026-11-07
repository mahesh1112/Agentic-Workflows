#!/usr/bin/env python3
"""
Repo Darwin: natural selection pre-step.

Runs before the GPT agent. It is deterministic and uses no LLM, so every
"decision" the agent receives can be audited from GitHub data alone.

What it does:
  1. Collects every Darwin PR (label `darwin`) and reads its fate:
       merged                     -> SURVIVED  (reward 1)
       closed by a human          -> EXTINCT   (reward 0)
       closed by a bot (expired)  -> IGNORED   (weak negative)
       open                       -> PENDING
  2. Pulls out every human comment and review on those PRs. These are the
     "taste signals" the agent learns the maintainer's preferences from.
  3. Runs Thompson sampling (a Bayesian multi-armed bandit) over mutation
     species: Beta(1 + survived, 1 + extinct + 0.5 * ignored).
     The sample decides which kind of mutation to attempt this generation
     and whether that choice is exploitation or exploration.
  4. Writes selection-pressure.json and selection-pressure.md for the agent,
     and the markdown also goes to the GitHub step summary.

Local testing: set DARWIN_FIXTURE=/path/to/prs.json (a list shaped like the
enriched PR records built below) to skip the GitHub API.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The species of mutation Darwin can attempt. Keys are stable identifiers
# because they are parsed back out of PR titles and bodies.
SPECIES: dict[str, str] = {
    "bugfix": "Fix a real, reproducible correctness defect",
    "reliability": "Make something fail less: error handling, idempotency, edge cases, CI robustness",
    "tests": "Add a test harness or tests that pin down current behaviour",
    "security": "Remove a security or supply-chain risk: pinning, secrets, unsafe patterns",
    "performance": "Make something measurably faster or cheaper",
    "simplify": "Delete or simplify: dead code, duplication, needless complexity",
    "docs": "Make the repo understandable: README, usage, architecture notes",
    "dx": "Improve developer experience: tooling, scripts, config, ergonomics",
    "feature": "A small new capability the code is clearly reaching for",
}

DNA_RE = re.compile(
    r"Darwin-DNA:\s*gen=(?P<gen>\d+)\s+species=(?P<species>[a-z-]+)\s+mode=(?P<mode>[a-z-]+)",
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"\bG(?P<gen>\d+)\s*[·|:-]\s*(?P<species>[a-z-]+)", re.IGNORECASE)

OUT_DIR = Path(os.environ.get("DARWIN_OUT_DIR", "/tmp/gh-aw/agent/darwin"))
IGNORED_WEIGHT = 0.5
MAX_FEEDBACK_CHARS = 1500


# --------------------------------------------------------------------------
# GitHub access (via the gh CLI that ships on Actions runners)
# --------------------------------------------------------------------------

def gh_json(args: list[str], default):
    try:
        out = subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True, timeout=120
        ).stdout
        return json.loads(out) if out.strip() else default
    except Exception as exc:  # network hiccups must never kill evolution
        print(f"::warning::gh {' '.join(args[:3])} failed: {exc}", file=sys.stderr)
        return default


def is_bot(login: str | None) -> bool:
    if not login:
        return True
    login = login.lower()
    return (
        login.endswith("[bot]")
        or login.startswith("app/")
        or login in {"github-actions", "copilot", "dependabot"}
    )


def fetch_prs(repo: str) -> list[dict]:
    prs = gh_json(
        [
            "pr", "list", "--repo", repo, "--label", "darwin", "--state", "all",
            "--limit", "200",
            "--json", "number,title,state,body,url,createdAt,closedAt,mergedAt,comments,reviews",
        ],
        [],
    )
    for pr in prs:
        n = pr["number"]
        pr["inlineComments"] = [
            {"author": c.get("user", {}).get("login"), "body": c.get("body", ""), "path": c.get("path")}
            for c in gh_json(["api", f"repos/{repo}/pulls/{n}/comments?per_page=100"], [])
        ]
        pr["closedBy"] = None
        if pr["state"] == "CLOSED":
            events = gh_json(["api", f"repos/{repo}/issues/{n}/events?per_page=100"], [])
            closers = [e.get("actor", {}).get("login") for e in events if e.get("event") == "closed"]
            pr["closedBy"] = closers[-1] if closers else None
    return prs


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def parse_dna(pr: dict) -> tuple[int | None, str | None]:
    m = DNA_RE.search(pr.get("body") or "") or TITLE_RE.search(pr.get("title") or "")
    if not m:
        return None, None
    species = m.group("species").lower()
    return int(m.group("gen")), species if species in SPECIES else None


def fate_of(pr: dict) -> str:
    if pr.get("mergedAt") or pr.get("state") == "MERGED":
        return "survived"
    if pr.get("state") == "OPEN":
        return "pending"
    return "ignored" if is_bot(pr.get("closedBy")) else "extinct"


def human_feedback(pr: dict) -> list[dict]:
    fb = []
    for c in pr.get("comments") or []:
        login = (c.get("author") or {}).get("login")
        if not is_bot(login) and (c.get("body") or "").strip():
            fb.append({"kind": "comment", "author": login, "body": c["body"][:MAX_FEEDBACK_CHARS]})
    for r in pr.get("reviews") or []:
        login = (r.get("author") or {}).get("login")
        if not is_bot(login) and ((r.get("body") or "").strip() or r.get("state") != "COMMENTED"):
            fb.append({
                "kind": f"review:{(r.get('state') or '').lower()}",
                "author": login,
                "body": (r.get("body") or "")[:MAX_FEEDBACK_CHARS],
            })
    for c in pr.get("inlineComments") or []:
        if not is_bot(c.get("author")) and (c.get("body") or "").strip():
            fb.append({
                "kind": f"inline:{c.get('path')}",
                "author": c.get("author"),
                "body": c["body"][:MAX_FEEDBACK_CHARS],
            })
    return fb


def thompson(stats: dict[str, dict], pending_species: set[str], seed: str) -> list[dict]:
    rng = random.Random(seed)
    ranked = []
    for sp, s in stats.items():
        a = 1 + s["survived"]
        b = 1 + s["extinct"] + IGNORED_WEIGHT * s["ignored"]
        ranked.append({
            "species": sp,
            "sample": round(rng.betavariate(a, b), 4),
            "posterior_mean": round(a / (a + b), 4),
            "alpha": a,
            "beta": b,
            # Diversity pressure: never stack two open PRs of the same species.
            "has_open_pr": sp in pending_species,
        })
    ranked.sort(key=lambda r: (r["has_open_pr"], -r["sample"], r["species"]))
    return ranked


def build(prs: list[dict], run_seed: str, focus: str) -> dict:
    stats = {sp: {"survived": 0, "extinct": 0, "ignored": 0, "pending": 0} for sp in SPECIES}
    lineage, feedback, pending_species = [], [], set()
    max_gen = 0

    for pr in sorted(prs, key=lambda p: p.get("createdAt") or ""):
        gen, species = parse_dna(pr)
        fate = fate_of(pr)
        if gen:
            max_gen = max(max_gen, gen)
        if species:
            stats[species][fate] += 1
            if fate == "pending":
                pending_species.add(species)
        fb = human_feedback(pr)
        lineage.append({
            "pr": pr["number"], "url": pr.get("url"), "title": pr.get("title"),
            "gen": gen, "species": species, "fate": fate,
            "created": pr.get("createdAt"), "closed": pr.get("closedAt"),
            "closed_by": pr.get("closedBy"), "human_feedback_count": len(fb),
        })
        for f in fb:
            feedback.append({"pr": pr["number"], "gen": gen, "species": species, "fate": fate, **f})

    ranked = thompson(stats, pending_species, run_seed)
    eligible = [r for r in ranked if not r["has_open_pr"]] or ranked
    best_mean = max(r["posterior_mean"] for r in eligible)
    chosen = ranked[0]
    decided = sum(s["survived"] + s["extinct"] + s["ignored"] for s in stats.values())

    if focus:
        mode = "focus"
    elif decided < 3:
        mode = "cold-start"
    elif chosen["posterior_mean"] >= best_mean:
        mode = "exploit"
    else:
        mode = "explore"

    survived = sum(s["survived"] for s in stats.values())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generation": max_gen + 1,
        "mode": mode,
        "focus": focus or None,
        "chosen_species": chosen["species"],
        "species_ranking": ranked,
        "species_catalog": SPECIES,
        "fitness": {
            "total_prs": len(prs),
            "decided": decided,
            "survived": survived,
            "survival_rate": round(survived / decided, 3) if decided else None,
            "by_species": stats,
        },
        "lineage": lineage,
        "human_feedback": feedback,
        "open_pr_count": sum(1 for l in lineage if l["fate"] == "pending"),
    }


def to_markdown(sp: dict) -> str:
    f = sp["fitness"]
    rate = f"{f['survival_rate']:.0%}" if f["survival_rate"] is not None else "n/a"
    lines = [
        f"# 🧬 Selection pressure: Generation {sp['generation']}",
        "",
        f"- **Mode:** `{sp['mode']}`" + (f" (focus: _{sp['focus']}_)" if sp["focus"] else ""),
        f"- **Chosen species:** `{sp['chosen_species']}`: {SPECIES[sp['chosen_species']]}",
        f"- **Fitness so far:** {f['survived']} survived / {f['decided']} decided (survival rate {rate}), "
        f"{sp['open_pr_count']} pending",
        "",
        "## Thompson sampling over species",
        "",
        "| Rank | Species | Sample | Posterior mean | α | β | Open PR |",
        "|---:|---|---:|---:|---:|---:|:---:|",
    ]
    for i, r in enumerate(sp["species_ranking"], 1):
        lines.append(
            f"| {i} | `{r['species']}` | {r['sample']:.3f} | {r['posterior_mean']:.3f} "
            f"| {r['alpha']:g} | {r['beta']:g} | {'●' if r['has_open_pr'] else ''} |"
        )
    lines += ["", "## Lineage", ""]
    if not sp["lineage"]:
        lines.append("_No ancestors yet. This is the first generation._")
    for l in sp["lineage"]:
        icon = {"survived": "🟢", "extinct": "🔴", "ignored": "⚪", "pending": "🟡"}[l["fate"]]
        lines.append(
            f"- {icon} G{l['gen'] or '?'} `{l['species'] or '?'}` #{l['pr']} **{l['fate']}**"
            f" ({l['human_feedback_count']} human signals): {l['title']}"
        )
    lines += ["", f"## Human taste signals ({len(sp['human_feedback'])})", ""]
    for fb in sp["human_feedback"]:
        body = " ".join(fb["body"].split())[:300]
        lines.append(f"- #{fb['pr']} ({fb['fate']}, {fb['kind']}): “{body}”")
    return "\n".join(lines) + "\n"


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    fixture = os.environ.get("DARWIN_FIXTURE")
    if fixture:
        prs = json.loads(Path(fixture).read_text(encoding="utf-8"))
    elif repo:
        prs = fetch_prs(repo)
    else:
        print("GITHUB_REPOSITORY or DARWIN_FIXTURE is required", file=sys.stderr)
        return 2

    seed = f"{repo}:{os.environ.get('GITHUB_RUN_ID', 'local')}"
    pressure = build(prs, seed, (os.environ.get("DARWIN_FOCUS") or "").strip())
    md = to_markdown(pressure)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "selection-pressure.json").write_text(json.dumps(pressure, indent=2), encoding="utf-8")
    (OUT_DIR / "selection-pressure.md").write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
