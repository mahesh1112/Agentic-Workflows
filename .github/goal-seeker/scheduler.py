#!/usr/bin/env python3
"""
Goal Seeker: deterministic scheduler (runs before the agent, no LLM).

Loop-engineering responsibilities handled here rather than in the prompt:
  * Item selection: pick exactly ONE goal issue per run, round-robin by
    least-recently-advanced, unless a /goal command or dispatch input names one.
  * Contract parsing: objective, success criteria (with verification commands),
    constraints and iteration budget from the issue body.
  * Run-state from GitHub: iteration count, last statuses, failure signatures
    (the circuit breaker) read from markers in the workflow's own comments.
  * Canonical branch: find the single PR on `goal/<n>` and check it out.

Writes /tmp/gh-aw/agent/goal/selection.json and selection.md.
Local testing: GOAL_FIXTURE=/path/fixture.json, shaped as {"issues": [...], "prs": [...]}
(issues as returned by `gh issue list --json ...`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(os.environ.get("GOAL_OUT_DIR", "/tmp/gh-aw/agent/goal"))
DEFAULT_BUDGET = 12
STATE_LABELS = {
    "goal:completed": "completed",
    "goal:paused": "paused",
    "goal:blocked": "blocked",
    "goal:needs-action": "needs_action",
}
MARKER_RE = re.compile(
    r"Goal-Seeker:\s*iteration=(?P<it>\d+)\s+status=(?P<status>[a-z_]+)(?:\s+signature=(?P<sig>\S+))?",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^#{2,4}\s+(?P<title>.+?)\s*$")
CHECKBOX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[(?P<mark>[ xX])\]\s+)?(?P<text>.+)$")
COMMAND_RE = re.compile(r"`([^`]+)`")


# ---------------------------------------------------------------- GitHub ----

def gh_json(args: list[str], default):
    try:
        out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, timeout=120).stdout
        return json.loads(out) if out.strip() else default
    except Exception as exc:
        print(f"::warning::gh {' '.join(args[:3])} failed: {exc}", file=sys.stderr)
        return default


def is_bot(login: str | None) -> bool:
    login = (login or "").lower()
    return not login or login.endswith("[bot]") or login.startswith("app/") or login == "github-actions"


def fetch(repo: str) -> tuple[list[dict], list[dict]]:
    issues = gh_json(
        ["issue", "list", "--repo", repo, "--label", "goal", "--state", "open", "--limit", "50",
         "--json", "number,title,body,url,labels,createdAt,updatedAt,comments"],
        [],
    )
    prs = gh_json(
        ["pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
         "--json", "number,title,url,headRefName,isDraft"],
        [],
    )
    return issues, prs


# --------------------------------------------------------------- Parsing ----

def sections(body: str) -> dict[str, str]:
    """Split an issue body (hand-written or issue-form) into heading -> text."""
    out, current, buf = {}, "_preamble", []
    for line in (body or "").splitlines():
        m = HEADING_RE.match(line)
        if m:
            out[current] = "\n".join(buf).strip()
            current, buf = m.group("title").strip().lower(), []
        else:
            buf.append(line)
    out[current] = "\n".join(buf).strip()
    return out


def find(secs: dict[str, str], *keys: str) -> str:
    for title, text in secs.items():
        if any(k in title for k in keys):
            return text
    return ""


def parse_contract(body: str) -> dict:
    secs = sections(body)
    objective = find(secs, "objective", "goal")
    criteria = []
    for line in find(secs, "success criteria", "done when", "definition of done", "criteria").splitlines():
        m = CHECKBOX_RE.match(line)
        if not m or m.group("text").strip() in {"_No response_", ""}:
            continue
        text = m.group("text").strip()
        cmd = COMMAND_RE.search(text)
        criteria.append({
            "id": f"C{len(criteria) + 1}",
            "text": text,
            "command": cmd.group(1) if cmd else None,
            "checked": (m.group("mark") or " ").lower() == "x",
        })
    budget_text = find(secs, "budget", "max iterations")
    budget_match = re.search(r"\d+", budget_text)
    budget = max(1, min(int(budget_match.group()), 50)) if budget_match else DEFAULT_BUDGET

    problems = []
    if len(objective) < 15:
        problems.append("Objective is missing or too vague (needs a sentence describing the end state).")
    if not criteria:
        problems.append("No success criteria found (add a checklist under a 'Success criteria' heading).")
    verifiable = sum(1 for c in criteria if c["command"])
    if criteria and verifiable == 0:
        problems.append("No success criterion has a verification command in `backticks`.")
    return {
        "objective": objective,
        "criteria": criteria,
        "constraints": find(secs, "constraint", "non-goal", "out of scope"),
        "budget": budget,
        "verifiable_ratio": round(verifiable / len(criteria), 2) if criteria else 0.0,
        "problems": problems,
        "ok": not problems,
    }


def parse_command(text: str) -> dict:
    m = re.search(r"(?:^|\s)/goal\b(?P<rest>.*)", text or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return {"kind": "none", "text": ""}
    rest = m.group("rest").strip()
    word = rest.split(maxsplit=1)[0].lower() if rest else ""
    if word in {"pause", "resume", "status"}:
        return {"kind": word, "text": rest}
    return {"kind": "steer" if rest else "advance", "text": rest}


def run_state(issue: dict) -> dict:
    iterations, statuses, signatures, last_at, human = 0, [], [], None, []
    for c in issue.get("comments") or []:
        login = (c.get("author") or {}).get("login")
        body = c.get("body") or ""
        m = MARKER_RE.search(body)
        if m and is_bot(login):
            iterations = max(iterations, int(m.group("it")))
            statuses.append(m.group("status").lower())
            signatures.append(m.group("sig"))
            last_at = c.get("createdAt")
        elif not is_bot(login) and body.strip():
            human.append({"author": login, "at": c.get("createdAt"), "body": body[:1200]})
    recent_sigs = [s for s in signatures[-3:] if s]
    stuck = len(recent_sigs) >= 2 and len(set(recent_sigs[-2:])) == 1 and statuses[-1] == "rejected"
    return {
        "iterations_done": iterations,
        "last_iteration_at": last_at,
        "recent_statuses": statuses[-5:],
        "recent_signatures": signatures[-5:],
        "stuck": stuck,
        "recent_human_comments": human[-8:],
    }


# ------------------------------------------------------------- Selection ----

def label_state(issue: dict, last_iteration_at: str | None) -> str:
    names = {(l.get("name") or "").lower() for l in issue.get("labels") or []}
    for label, state in STATE_LABELS.items():
        if label in names:
            # A goal parked for a better contract re-enters the loop once a human
            # edits or comments on it after the agent's last iteration.
            if state == "needs_action" and last_iteration_at and (issue.get("updatedAt") or "") > last_iteration_at:
                return "active"
            return state
    return "active"


def select(issues: list[dict], prs: list[dict], target: int | None, command: dict) -> dict:
    enriched = []
    for issue in issues:
        contract = parse_contract(issue.get("body") or "")
        state = run_state(issue)
        branch = f"goal/{issue['number']}"
        pr = next((p for p in prs if p.get("headRefName") == branch), None)
        enriched.append({
            "number": issue["number"],
            "title": issue["title"],
            "url": issue.get("url"),
            "labels": [l.get("name") for l in issue.get("labels") or []],
            "status": label_state(issue, state["last_iteration_at"]),
            "branch": branch,
            "pr": pr,
            "contract": contract,
            **state,
            "budget_exhausted": state["iterations_done"] >= contract["budget"],
        })

    chosen = None
    if target is not None:
        chosen = next((g for g in enriched if g["number"] == target), None)
        reason = f"explicitly requested (#{target})" if chosen else f"#{target} is not an open goal issue"
    else:
        runnable = [g for g in enriched if g["status"] == "active" and not g["budget_exhausted"]]
        # Never-advanced goals first, then least-recently-advanced (round-robin fairness).
        runnable.sort(key=lambda g: (g["last_iteration_at"] is not None, g["last_iteration_at"] or "", g["number"]))
        chosen = runnable[0] if runnable else None
        reason = "least recently advanced active goal" if chosen else "no active goal with budget left"

    return {
        "command": command,
        "selection_reason": reason,
        "goal": chosen,
        "deferred": [
            {"number": g["number"], "title": g["title"], "status": g["status"],
             "iterations_done": g["iterations_done"], "budget": g["contract"]["budget"]}
            for g in enriched if not chosen or g["number"] != chosen["number"]
        ],
    }


def checkout_branch(goal: dict, repo: str) -> dict:
    """Check out the goal's canonical branch so the agent iterates on top of it."""
    if not goal or not goal.get("pr"):
        return {"checked_out": False, "reason": "no existing PR; agent creates the branch"}
    branch, token = goal["branch"], os.environ.get("GH_TOKEN", "")
    url = f"https://x-access-token:{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"
    try:
        # The workflow's `checkout.fetch: ["goal/*"]` normally provides the ref already;
        # fetch it ourselves only as a fallback.
        have_ref = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
                                  capture_output=True, text=True).returncode == 0
        if not have_ref:
            subprocess.run(["git", "fetch", "--no-tags", "--depth=50", url,
                            f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                           check=True, capture_output=True, text=True, timeout=120)
        subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"],
                       check=True, capture_output=True, text=True, timeout=60)
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        return {"checked_out": True, "head": head}
    except Exception as exc:  # do not leak the token-bearing URL into the error
        return {"checked_out": False, "reason": f"fetch/checkout of {branch} failed ({type(exc).__name__})"}


# ---------------------------------------------------------------- Output ----

def bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def to_markdown(sel: dict) -> str:
    g = sel["goal"]
    lines = ["# 🎯 Goal Seeker: selection", "",
             f"- **Command:** `{sel['command']['kind']}` {sel['command']['text']}".rstrip(),
             f"- **Selection:** {sel['selection_reason']}"]
    if g:
        c = g["contract"]
        lines += [
            f"- **Goal:** #{g['number']} {g['title']} (status `{g['status']}`)",
            f"- **Iterations:** {g['iterations_done']} / {c['budget']} {bar(g['iterations_done'], c['budget'])}",
            f"- **Contract OK:** {c['ok']} (verifiable criteria: {c['verifiable_ratio']:.0%})",
            f"- **Stuck (repeated failure signature):** {g['stuck']}",
            f"- **PR:** {g['pr']['url'] if g['pr'] else 'none yet'} · branch `{g['branch']}` "
            f"· checked out: {g.get('checkout', {}).get('checked_out')}",
            "", "| ID | Criterion | Verifier |", "|---|---|---|",
        ]
        esc = lambda s: s.replace("|", "\\|")
        for cr in c["criteria"]:
            verifier = f"`{esc(cr['command'])}`" if cr["command"] else "—"
            lines.append(f"| {cr['id']} | {esc(cr['text'])} | {verifier} |")
        for p in c["problems"]:
            lines.append(f"\n> ⚠️ {p}")
    if sel["deferred"]:
        lines += ["", "**Deferred goals:** " + ", ".join(f"#{d['number']} ({d['status']})" for d in sel["deferred"])]
    return "\n".join(lines) + "\n"


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    fixture = os.environ.get("GOAL_FIXTURE")
    if fixture:
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        issues, prs = data["issues"], data.get("prs", [])
    elif repo:
        issues, prs = fetch(repo)
    else:
        print("GITHUB_REPOSITORY or GOAL_FIXTURE is required", file=sys.stderr)
        return 2

    command = parse_command(os.environ.get("GOAL_COMMENT", ""))
    target_raw = (os.environ.get("GOAL_ISSUE") or "").strip().lstrip("#")
    if command["kind"] != "none" and os.environ.get("GOAL_EVENT_ISSUE"):
        target_raw = os.environ["GOAL_EVENT_ISSUE"]
    target = int(target_raw) if target_raw.isdigit() else None

    sel = select(issues, prs, target, command)
    if sel["goal"] and not fixture:
        sel["goal"]["checkout"] = checkout_branch(sel["goal"], repo)

    md = to_markdown(sel)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "selection.json").write_text(json.dumps(sel, indent=2), encoding="utf-8")
    (OUT_DIR / "selection.md").write_text(md, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
