#!/usr/bin/env python3
import base64
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request


DEFAULT_JQL = "duedate < startOfDay() AND statusCategory != Done ORDER BY duedate ASC"
JIRA_FIELDS = ["summary", "duedate", "status"]
DISCORD_EMBEDS_PER_MESSAGE = 10


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def request_json(url: str, method: str, headers: dict[str, str], payload: dict | None = None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code} {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def jira_headers(email: str, api_token: str) -> dict[str, str]:
    token = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


def fetch_overdue_issues(base_url: str, email: str, api_token: str, jql: str, max_results: int) -> list[dict]:
    url = f"{base_url}/rest/api/3/search/jql"
    headers = jira_headers(email, api_token)
    issues: list[dict] = []
    next_page_token = None

    while True:
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": JIRA_FIELDS,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        result = request_json(url, "POST", headers, payload)
        batch = result.get("issues", [])
        issues.extend(batch)

        next_page_token = result.get("nextPageToken")
        if result.get("isLast", True) or not next_page_token:
            return issues


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def issue_to_embed(base_url: str, issue: dict) -> dict:
    key = issue["key"]
    fields = issue.get("fields", {})
    summary = fields.get("summary") or "(제목 없음)"
    due_date = fields.get("duedate") or "-"
    status = (fields.get("status") or {}).get("name") or "-"
    issue_url = f"{base_url}/browse/{key}"

    return {
        "title": truncate(f"{key} {summary}", 256),
        "url": issue_url,
        "color": 0xD83A34,
        "fields": [
            {"name": "마감일", "value": due_date, "inline": True},
            {"name": "상태", "value": status, "inline": True},
            {"name": "링크", "value": issue_url, "inline": False},
        ],
    }


def post_discord(webhook_url: str, payload: dict) -> None:
    request_json(webhook_url, "POST", {"Accept": "application/json"}, payload)


def send_report(webhook_url: str, base_url: str, issues: list[dict]) -> None:
    if not issues:
        post_discord(webhook_url, {"content": "Jira 연체 이슈: 없음"})
        return

    header = f"Jira 연체 이슈 {len(issues)}건"
    embeds = [issue_to_embed(base_url, issue) for issue in issues]

    for index in range(0, len(embeds), DISCORD_EMBEDS_PER_MESSAGE):
        chunk = embeds[index : index + DISCORD_EMBEDS_PER_MESSAGE]
        payload = {"embeds": chunk}
        if index == 0:
            payload["content"] = header
        post_discord(webhook_url, payload)


def main() -> int:
    base_url = require_env("JIRA_BASE_URL").rstrip("/")
    email = require_env("JIRA_EMAIL")
    api_token = require_env("JIRA_API_TOKEN")
    webhook_url = require_env("DISCORD_WEBHOOK_URL")
    jql = os.getenv("JIRA_JQL") or DEFAULT_JQL
    max_results = int(os.getenv("JIRA_MAX_RESULTS", "100"))

    print(
        textwrap.dedent(
            f"""\
            Fetching overdue Jira issues
            Base URL: {base_url}
            JQL: {jql}
            """
        ).strip()
    )

    issues = fetch_overdue_issues(base_url, email, api_token, jql, max_results)
    send_report(webhook_url, base_url, issues)
    print(f"Sent Discord report for {len(issues)} overdue issue(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
