#!/usr/bin/env python3
import base64
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from collections import OrderedDict


DEFAULT_JQL = "issuetype = Task AND created >= -365d ORDER BY status ASC, duedate ASC"
JIRA_FIELDS = ["summary", "duedate", "status", "issuetype"]
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_EMBED_DESCRIPTION_LIMIT = 3900
USER_AGENT = "schedule-tracking-jira-overdue-notifier/1.0"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def request_json(url: str, method: str, headers: dict[str, str], payload: dict | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT, **headers}
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


def fetch_jira_fields(base_url: str, email: str, api_token: str) -> list[dict]:
    url = f"{base_url}/rest/api/3/field"
    return request_json(url, "GET", jira_headers(email, api_token))


def resolve_start_date_field(base_url: str, email: str, api_token: str) -> str | None:
    configured_field = os.getenv("JIRA_START_DATE_FIELD")
    if configured_field:
        return configured_field

    for field in fetch_jira_fields(base_url, email, api_token):
        if (field.get("name") or "").strip().lower() == "start date":
            return field.get("id")
    return None


def fetch_overdue_issues(
    base_url: str,
    email: str,
    api_token: str,
    jql: str,
    max_results: int,
    fields: list[str],
) -> list[dict]:
    url = f"{base_url}/rest/api/3/search/jql"
    headers = jira_headers(email, api_token)
    issues: list[dict] = []
    next_page_token = None

    while True:
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields,
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


def issue_status(issue: dict) -> str | None:
    status = ((issue.get("fields") or {}).get("status") or {}).get("name")
    if not status:
        return None
    return status.strip() or None


def issue_type(issue: dict) -> str | None:
    issue_type_name = ((issue.get("fields") or {}).get("issuetype") or {}).get("name")
    if not issue_type_name:
        return None
    return issue_type_name.strip() or None


def date_value(fields: dict, field_name: str | None) -> str:
    if not field_name:
        return "-"
    value = fields.get(field_name)
    if not value:
        return "-"
    if isinstance(value, str):
        return value[:10]
    return str(value)


def issue_line(base_url: str, issue: dict, start_date_field: str | None) -> str:
    key = issue["key"]
    fields = issue.get("fields", {})
    summary = fields.get("summary") or "(제목 없음)"
    start_date = date_value(fields, start_date_field)
    due_date = date_value(fields, "duedate")
    issue_url = f"{base_url}/browse/{key}"
    title = truncate(summary.replace("\n", " "), 120)
    return f"- {title}: {start_date} - {due_date} ([link]({issue_url}))"


def grouped_embeds(base_url: str, issues: list[dict], start_date_field: str | None) -> list[dict]:
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    for issue in issues:
        status = issue_status(issue)
        if not status:
            continue
        grouped.setdefault(status, []).append(issue_line(base_url, issue, start_date_field))

    embeds: list[dict] = []
    for status, lines in grouped.items():
        chunk: list[str] = []
        chunk_length = 0
        for line in lines:
            next_length = chunk_length + len(line) + 1
            if chunk and next_length > DISCORD_EMBED_DESCRIPTION_LIMIT:
                embeds.append(status_embed(status, chunk))
                chunk = []
                chunk_length = 0
            chunk.append(line)
            chunk_length += len(line) + 1
        if chunk:
            embeds.append(status_embed(status, chunk))
    return embeds


def status_embed(status: str, lines: list[str]) -> dict:
    return {
        "title": truncate(status, 256),
        "description": "\n".join(lines),
        "color": 0xD83A34,
    }


def print_issue_summary(issues: list[dict], start_date_field: str | None) -> None:
    for issue in issues:
        fields = issue.get("fields", {})
        summary = fields.get("summary") or "(제목 없음)"
        start_date = date_value(fields, start_date_field)
        due_date = date_value(fields, "duedate")
        status = issue_status(issue) or "-"
        issue_type_name = issue_type(issue) or "-"
        print(f"{issue['key']} | {issue_type_name} | {status} | {start_date} | {due_date} | {summary}")


def post_discord(webhook_url: str, payload: dict) -> None:
    request_json(
        webhook_url,
        "POST",
        {
            "Accept": "application/json",
            "User-Agent": "DiscordBot (schedule-tracking, 1.0)",
        },
        payload,
    )


def send_report(webhook_url: str, base_url: str, issues: list[dict], start_date_field: str | None) -> None:
    embeds = grouped_embeds(base_url, issues, start_date_field)

    if not embeds:
        return

    header = "Jira Status"

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
    dry_run = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}
    start_date_field = resolve_start_date_field(base_url, email, api_token)
    fields = JIRA_FIELDS + ([start_date_field] if start_date_field else [])

    print(
        textwrap.dedent(
            f"""\
            Fetching overdue Jira issues
            Base URL: {base_url}
            JQL: {jql}
            Start date field: {start_date_field or "-"}
            """
        ).strip()
    )

    issues = fetch_overdue_issues(base_url, email, api_token, jql, max_results, fields)
    if dry_run:
        print_issue_summary(issues, start_date_field)
    else:
        send_report(webhook_url, base_url, issues, start_date_field)
    print(f"Fetched {len(issues)} issue(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
