#!/usr/bin/env python3
"""Fetch CDC measles data and update the project's Datawrapper charts.

The script uses only Python's standard library so the GitHub Action does not
need a package-install step. It writes a local snapshot and only calls
Datawrapper when the corresponding feed has changed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CDC_BASE = "https://www.cdc.gov/wcms/vizdata/measles"
FEEDS = {
    "weekly": f"{CDC_BASE}/MeaslesCasesWeekly.json",
    "map": f"{CDC_BASE}/MeaslesCasesMap.json",
    "annual": f"{CDC_BASE}/MeaslesCasesYear.json",
}
SOURCE_NAME = "Centers for Disease Control and Prevention"
BYLINE = "Annie Jennemann/Get the Facts Data Team"


def fetch_json(url: str) -> list[dict[str, str]]:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            # CDC's edge currently rejects urllib's default/browser-like
            # identity but accepts this conventional command-line client UA.
            "User-Agent": "curl/8.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise RuntimeError(f"Unexpected CDC response shape from {url}")
    return payload


def rows_to_csv(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    fieldnames = list(rows[0])
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def read_snapshot(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_snapshot(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text(rows_to_csv(rows), encoding="utf-8")


def changed(rows: list[dict[str, str]], path: Path) -> bool:
    return rows_to_csv(rows) != rows_to_csv(read_snapshot(path))


def normalize_weekly(raw: list[dict[str, str]], start_date: date) -> list[dict[str, str]]:
    rows = [
        {"week_end": row["week_end"], "cases": str(int(row["cases"]))}
        for row in raw
        if date.fromisoformat(row["week_start"]) >= start_date
    ]
    return sorted(rows, key=lambda row: row["week_end"])


def normalize_map(raw: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    years = [int(row["year"]) for row in raw if row.get("year", "").isdigit()]
    if not years:
        raise RuntimeError("CDC map feed did not contain a year")
    current_year = max(years)
    rows = [row for row in raw if int(row["year"]) == current_year]
    return sorted(rows, key=lambda row: row["geography"]), current_year


def normalize_annual(raw: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [row for row in raw if row.get("filter") == "2000-Present*"]
    if not rows:
        raise RuntimeError("CDC annual feed no longer contains the 2000-Present* series")
    return sorted(rows, key=lambda row: int(row["year"]))


class Datawrapper:
    def __init__(self, token: str, dry_run: bool = False) -> None:
        self.token = token
        self.dry_run = dry_run

    def request(self, method: str, path: str, body: bytes, content_type: str) -> None:
        if self.dry_run:
            print(f"DRY RUN: {method} {path} ({len(body)} bytes)")
            return
        request = Request(
            f"https://api.datawrapper.de{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
                "User-Agent": "measles-tracker/1.0",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Datawrapper returned HTTP {response.status}")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Datawrapper request failed for {path}: {exc}") from exc

    def update(self, chart_id: str, rows: list[dict[str, str]], title: str, chart_metadata: dict) -> None:
        if not chart_id:
            return
        self.request("PUT", f"/v3/charts/{chart_id}/data", rows_to_csv(rows).encode(), "text/csv")
        self.request("PATCH", f"/v3/charts/{chart_id}", json.dumps({"title": title, "metadata": chart_metadata}).encode(), "application/json")
        self.request("POST", f"/charts/{chart_id}/publish", b"", "application/json")


def metadata(intro: str, annotate: str, *, date_column: str | None = None, color: bool = False) -> dict:
    result = {
        "describe": {"source-name": SOURCE_NAME, "byline": BYLINE, "intro": intro},
        "annotate": {"notes": annotate},
        "publish": {"blocks": {"get-the-data": False}},
    }
    # Datawrapper stores the title at the top level, but metadata edits are
    # intentionally kept here; the existing charts already have their titles.
    if date_column:
        result["data"] = {"column-format": {date_column: {"type": "date"}}}
    if color:
        result["visualize"] = {"base-color": "#D6842F"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="fetch and compare data without API calls")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    token = os.getenv("DW_API_KEY", os.getenv("API_KEY", ""))
    charts = {
        "weekly": os.getenv("CHART_KEY", ""),
        "map": os.getenv("CHART_KEY1", ""),
        "annual": os.getenv("CHART_KEY2", ""),
    }
    if not args.dry_run and (not token or not all(charts.values())):
        print("DW_API_KEY, CHART_KEY, CHART_KEY1, and CHART_KEY2 are required", file=sys.stderr)
        return 2

    fetched = {name: fetch_json(url) for name, url in FEEDS.items()}
    today = date.today()
    # Match the original chart's rolling display window: last calendar year
    # through the latest CDC week.
    weekly = normalize_weekly(fetched["weekly"], date(today.year - 1, 1, 1))
    state, current_year = normalize_map(fetched["map"])
    annual = normalize_annual(fetched["annual"])
    total_current = sum(int(row["cases"]) for row in annual if int(row["year"]) == current_year)
    updated = today.strftime("%B %-d")

    jobs = [
        ("weekly", weekly, root / "last_weekly_us.csv", "Confirmed weekly cases of measles in the U.S.", metadata(
            f"There have been {total_current:,} positive measles cases in {current_year}.",
            f"<i>Chart updated {updated}<br>Data by last day of the week.</i>",
            date_column="week_end", color=True)),
        ("map", state, root / "last_map_us.csv", f"Confirmed measles cases by state in {current_year}", metadata(
            f"There have been {sum(row['cases_range'] != '0' for row in state)} states with positive cases of measles in {current_year}.",
            f"Chart updated {updated}.")),
        ("annual", annual, root / "last_annual_us.csv", f"Confirmed measles cases by year, 2000-{current_year}", metadata(
            f"There have been {total_current:,} positive measles cases in {current_year}.",
            f"<i>Chart updated {updated}</i>", color=True)),
    ]

    client = Datawrapper(token, args.dry_run)
    for name, rows, snapshot, title, chart_metadata in jobs:
        is_changed = changed(rows, snapshot)
        print(f"{name}: {'changed' if is_changed else 'unchanged'} ({len(rows)} rows)")
        if is_changed:
            if not args.dry_run:
                write_snapshot(snapshot, rows)
            client.update(charts[name], rows, title, chart_metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
