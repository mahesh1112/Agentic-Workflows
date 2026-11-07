#!/usr/bin/env python3
"""
CI Healer: deterministic failure collector (runs before the agent, no LLM).

Turns a failed workflow run into a compact incident file:
  * failing job/step names, head SHA, URL;
  * the failed-step log, de-timestamped and trimmed;
  * a STABLE failure key  <workflow>:<job>:<step>:<hash of normalised error>
    so that the same root cause always maps to the same incident (dedup) and the
    agent's casebook (episodic memory) can recall how it was fixed last time;
  * whether an open incident issue already exists for that key;
  * recent conclusions of the same workflow (a flakiness signal).

Writes /tmp/gh-aw/agent/ci/failure.json and failed.log.
Local testing: CI_FIXTURE_DIR=<dir with run.json, failed.log, issues.json, history.json>.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path(os.environ.get("CI_OUT_DIR", "/tmp/gh-aw/agent/ci"))
MAX_LOG_LINES = 400
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
ERROR_RE = re.compile(r"(error|exception|traceback|failed|fatal|exit code [1-9]|assert)", re.IGNORECASE)
# Runner boilerplate that appears in every failure; useless as a root-cause anchor.
GENERIC_RE = re.compile(r"Process completed with exit code|^##\[error\]$|Error: Process completed", re.IGNORECASE)
NOISE = [
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"(/[\w.@-]+){2,}"), "<path>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
]


def sh(args: list[str], fixture: str | None = None, as_json: bool = True, default=None):
    fx = os.environ.get("CI_FIXTURE_DIR")
    if fx and fixture:
        p = Path(fx) / fixture
        if not p.exists():
            return default
        text = p.read_text(encoding="utf-8")
        return json.loads(text) if as_json else text
    try:
        out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, timeout=180).stdout
        return (json.loads(out) if out.strip() else default) if as_json else out
    except Exception as exc:
        print(f"::warning::gh {' '.join(args[:3])} failed: {exc}", file=sys.stderr)
        return default


def normalise(line: str) -> str:
    for rx, repl in NOISE:
        line = rx.sub(repl, line)
    return " ".join(line.split())[:200]


def parse_log(raw: str) -> list[dict]:
    """`gh run view --log-failed` lines look like: job<TAB>step<TAB>timestamp message."""
    rows = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            job, step, msg = parts
        else:
            job, step, msg = "", "", line
        rows.append({"job": job.strip(), "step": step.strip(), "msg": TS_RE.sub("", msg).rstrip()})
    return rows


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = (os.environ.get("CI_RUN_ID") or "").strip()
    if not run_id:
        latest = sh(["run", "list", "--repo", repo, "--status", "failure", "--limit", "1",
                     "--json", "databaseId"], "latest.json", default=[])
        run_id = str(latest[0]["databaseId"]) if latest else ""
    if not run_id:
        print("No failed run to analyse.")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "failure.json").write_text(json.dumps({"found": False}), encoding="utf-8")
        return 0

    run = sh(["run", "view", run_id, "--repo", repo, "--json",
              "databaseId,name,workflowName,conclusion,headSha,headBranch,event,url,createdAt,displayTitle,jobs"],
             "run.json", default={}) or {}
    raw_log = sh(["run", "view", run_id, "--repo", repo, "--log-failed"], "failed.log", as_json=False, default="") or ""
    rows = parse_log(raw_log)

    failed_jobs = [j for j in run.get("jobs") or [] if j.get("conclusion") not in (None, "success", "skipped")]
    first_job = failed_jobs[0] if failed_jobs else {}
    failed_step = next((s for s in first_job.get("steps") or [] if s.get("conclusion") == "failure"), {})
    error_lines = [r["msg"] for r in rows if ERROR_RE.search(r["msg"]) and not GENERIC_RE.search(r["msg"])]
    anchor = error_lines[-1] if error_lines else (rows[-1]["msg"] if rows else "no-log")

    workflow = run.get("workflowName") or run.get("name") or "unknown-workflow"
    job_name = first_job.get("name") or (rows[0]["job"] if rows else "unknown-job")
    step_name = failed_step.get("name") or (rows[0]["step"] if rows else "unknown-step")
    digest = hashlib.sha1(normalise(anchor).encode()).hexdigest()[:10]
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]
    key = f"{slug(workflow)}:{slug(job_name)}:{slug(step_name)}:{digest}"

    existing = sh(["issue", "list", "--repo", repo, "--label", "ci-healer", "--state", "open",
                   "--search", f'"{key}" in:body', "--json", "number,title,url"], "issues.json", default=[]) or []
    history = sh(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", "10",
                  "--json", "conclusion,headSha,createdAt"], "history.json", default=[]) or []
    same_sha_passed = any(h.get("conclusion") == "success" and h.get("headSha") == run.get("headSha") for h in history)

    tail = rows[-MAX_LOG_LINES:]
    incident = {
        "found": True,
        "failure_key": key,
        "run": {k: run.get(k) for k in ("databaseId", "workflowName", "conclusion", "headSha", "headBranch",
                                        "event", "url", "createdAt", "displayTitle")},
        "failed_jobs": [{"name": j.get("name"), "conclusion": j.get("conclusion"),
                         "failed_steps": [s.get("name") for s in j.get("steps") or [] if s.get("conclusion") == "failure"]}
                        for j in failed_jobs],
        "anchor_error": anchor,
        "error_lines": error_lines[-15:],
        "existing_incident": existing[0] if existing else None,
        "recent_conclusions": [h.get("conclusion") for h in history],
        "flake_signal": same_sha_passed,
        "log_lines_total": len(rows),
        "log_file": str(OUT_DIR / "failed.log"),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "failed.log").write_text(
        "\n".join(f"[{r['job']} › {r['step']}] {r['msg']}" for r in tail), encoding="utf-8")
    (OUT_DIR / "failure.json").write_text(json.dumps(incident, indent=2), encoding="utf-8")
    summary = (f"## 🩺 CI incident\n\n- **Key:** `{key}`\n- **Run:** {run.get('url')}\n"
               f"- **Anchor error:** `{anchor[:180]}`\n- **Existing incident:** "
               f"{existing[0]['url'] if existing else 'none'}\n- **Flake signal:** {same_sha_passed}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(summary)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
