---
on:
  schedule:
    - cron: "0 7 * * *"  # Every day at 07:00 UTC
permissions:
  contents: read
  issues: read
  pull-requests: read
safe-outputs:
  create-issue:
    title-prefix: "[Daily Report] "
    labels: [report, daily-status]
---
# Daily Repo Status Report

Generate a daily summary report for GitHub issues, pull requests, discussions, releases,
and recent code changes. Create a GitHub issue with:

- Overall progress highlights
- Key activities and trends
- Blockers and recommendations
