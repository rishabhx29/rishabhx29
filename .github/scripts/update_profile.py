#!/usr/bin/env python3
"""Refresh the generated blocks in the GitHub profile README.

The README carries four generated sections, each fenced by HTML comment
markers. This script rewrites the contents between the markers so the page
stays accurate without a human editing it:

    <!-- PROJECTS:START --> ... <!-- PROJECTS:END -->
    <!-- OSS:START -->      ... <!-- OSS:END -->
    <!-- PRS:START -->      ... <!-- PRS:END -->
    <!-- STATS:START -->    ... <!-- STATS:END -->

Everything outside the markers is hand-written prose and is never touched.

If the GitHub API is unreachable or rate-limited, each block is left exactly
as it was, so a failed run can never make the README worse.

Run by .github/workflows/update-profile.yml. Local dry run:

    python .github/scripts/update_profile.py --dry-run
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

USER = os.environ.get("GH_USER", "rishabhx29")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
API = "https://api.github.com"
README = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "README.md")

# ---------------------------------------------------------------------------
# Configuration. To add a project or an upstream project, edit here and the
# README follows on the next run.
# ---------------------------------------------------------------------------

FEATURED = [
    {
        "repo": "tessera",
        "blurb": "Local-first search engine that OCRs and embeds every image on your machine",
        "stack": "Rust, Tauri, ONNX Runtime, SQLite",
    },
    {
        "repo": "edgemem",
        "blurb": "Disagreement-durable edge memory engine, where a claim is an assertion and never an overwritten fact",
        "stack": "Python, Qdrant Edge, WebSockets",
    },
    {
        "repo": "Traceon",
        "blurb": "Codebase analyzer that parses full ASTs and renders interactive dependency graphs",
        "stack": "Next.js, TypeScript, React Flow, MongoDB",
    },
    {
        "repo": "velokey",
        "blurb": "Real-time multiplayer typing races over WebSockets, with server-side anti-cheat",
        "stack": "Next.js, TypeScript, PartyKit, Web Audio",
    },
]

# Only upstream projects worth naming on a profile go in this list. Class
# projects, forks and personal experiments are deliberately excluded.
WATCHED = [
    {
        "repo": "mantinedev/mantine",
        "label": "Mantine",
        "note": "React component library, 32k stars",
    },
    {
        "repo": "pingdotgg/t3code",
        "label": "T3 Code",
        "note": "Theo Browne's AI coding agent",
    },
]

RECENT_PRS = 10

# GitHub strips target="_blank" from README links, so a link cannot be forced
# to open in a new tab from the README itself. Every link is routed through
# this interstitial instead: it opens the destination in a new tab and then
# sends the reader back to the profile.
LINK_BASE = "https://www.rishabhx29.me/go.html"


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------


def api_get(path: str, params: dict | None = None, attempts: int = 3) -> dict | list | None:
    """GET a GitHub API path. Returns None on any failure, never raises.

    A single flaky connection must not take the whole run down, so transport
    errors are retried a few times and then reported as "no data" rather than
    as a crash. The caller keeps the previous block content in that case.
    """
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-readme-bot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_error = exc
            # A rate limit or a server error is worth another try; a 404 or
            # 401 will not become a 200 on the next attempt.
            status = getattr(exc, "code", None)
            if status and status not in (403, 429, 500, 502, 503, 504):
                break
        except (OSError, http.client.HTTPException, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(2 * attempt)

    print(f"  ! GET {path} failed: {last_error}", file=sys.stderr)
    return None


def repo_stats(owner: str, repo: str) -> dict | None:
    return api_get(f"/repos/{owner}/{repo}")


def search_prs(query: str, per_page: int = 30) -> dict | None:
    return api_get("/search/issues", {"q": query, "per_page": per_page, "sort": "updated", "order": "desc"})


def merged_pr_count(query: str) -> int:
    result = search_prs(query, per_page=1)
    if not isinstance(result, dict):
        return -1
    return int(result.get("total_count", -1))


def all_public_repos(user: str) -> list[dict]:
    repos: list[dict] = []
    for page in range(1, 6):
        batch = api_get(f"/users/{user}/repos", {"per_page": 100, "page": page, "type": "owner"})
        if not isinstance(batch, list) or not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
    return repos


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def link(url: str, text: str) -> str:
    """An anchor that opens its destination in a new tab.

    GitHub removes target="_blank" when it renders a README, so the link
    points at the interstitial on the portfolio site. That page opens the
    real destination in a new tab and then returns the reader to the profile.
    """
    dest = urllib.parse.quote(url, safe="")
    return f'<a href="{LINK_BASE}?u={dest}" target="_blank" rel="noopener">{text}</a>'


def render_projects(user: str) -> str | None:
    rows = []
    for entry in FEATURED:
        stats = repo_stats(user, entry["repo"])
        if stats is None:
            return None
        stars = stats.get("stargazers_count", 0)
        url = f"https://github.com/{user}/{entry['repo']}"
        rows.append(
            f'| {link(url, entry["repo"])} '
            f'| {entry["blurb"]} '
            f'| {entry["stack"]} '
            f'| {stars} |'
        )
    header = "| Project | What it does | Stack | Stars |\n| --- | --- | --- | --- |"
    return header + "\n" + "\n".join(rows)


def render_oss(user: str) -> str | None:
    rows = []
    for entry in WATCHED:
        count = merged_pr_count(f"author:{user} type:pr is:merged repo:{entry['repo']}")
        if count < 0:
            return None
        url = f"https://github.com/{entry['repo']}"
        rows.append(
            f'| {link(url, entry["label"])} '
            f'| {entry["note"]} '
            f'| {count} |'
        )
    header = "| Upstream project | What it is | Merged PRs |\n| --- | --- | --- |"
    return header + "\n" + "\n".join(rows)


def render_recent_prs(user: str) -> str | None:
    collected: list[dict] = []
    for entry in WATCHED:
        result = search_prs(f"author:{user} type:pr is:merged repo:{entry['repo']}", per_page=RECENT_PRS)
        if not isinstance(result, dict):
            return None
        for item in result.get("items", []):
            merged_at = (item.get("pull_request") or {}).get("merged_at")
            if not merged_at:
                continue
            collected.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title", "").strip(),
                    "url": item.get("html_url", ""),
                    "repo": entry["repo"],
                    "label": entry["label"],
                    "merged_at": merged_at,
                }
            )

    if not collected:
        return "_No merged pull requests yet, this section updates itself._"

    collected.sort(key=lambda pr: pr["merged_at"], reverse=True)
    lines = []
    for pr in collected[:RECENT_PRS]:
        when = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        stamp = when.strftime("%d %b %Y")
        title = pr["title"].replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
        lines.append(
            f'- {link(pr["url"], "#" + str(pr["number"]))} {title} '
            f'<sub>{pr["label"]} &middot; {stamp}</sub>'
        )
    return "\n".join(lines)


def render_stats(user: str) -> str | None:
    profile = api_get(f"/users/{user}")
    if not isinstance(profile, dict):
        return None

    repos = all_public_repos(user)
    if not repos:
        return None
    stars = sum(repo.get("stargazers_count", 0) for repo in repos)
    merged = merged_pr_count(f"author:{user} type:pr is:merged")
    upstream = len(WATCHED)

    header = (
        "| Public repos | Stars | Merged PRs | Upstream projects | Followers |\n"
        "| --- | --- | --- | --- | --- |"
    )
    row = (
        f"| {len(repos)} | {stars} | {merged} | {upstream} "
        f"| {profile.get('followers', 0)} |"
    )
    return header + "\n" + row


# ---------------------------------------------------------------------------
# README rewriting
# ---------------------------------------------------------------------------

BLOCKS = {
    "PROJECTS": render_projects,
    "OSS": render_oss,
    "PRS": render_recent_prs,
    "STATS": render_stats,
}


def replace_block(readme: str, name: str, content: str) -> str:
    pattern = re.compile(
        rf"(<!--\s*{name}:START\s*-->)(.*?)(<!--\s*{name}:END\s*-->)",
        re.DOTALL,
    )
    if not pattern.search(readme):
        print(f"  ! markers for {name} not found, skipping", file=sys.stderr)
        return readme
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", readme, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the result instead of writing it")
    args = parser.parse_args()

    if not os.path.exists(README):
        print(f"README not found at {README}", file=sys.stderr)
        return 1

    with open(README, "r", encoding="utf-8") as handle:
        readme = handle.read()

    updated = readme
    for name, renderer in BLOCKS.items():
        try:
            content = renderer(USER)
        except Exception as exc:  # never let one block take the run down
            print(f"  ! {name}: {exc}", file=sys.stderr)
            content = None
        if content is None:
            print(f"  = {name}: keeping the previous content (API unavailable)")
            continue
        updated = replace_block(updated, name, content)
        print(f"  + {name}: refreshed")

    if updated == readme:
        print("README already up to date")
        return 0

    if args.dry_run:
        print(updated)
        return 0

    with open(README, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    print(f"README updated: {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
