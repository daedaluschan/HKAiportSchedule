#!/usr/bin/env python3
"""Fetch yesterday's HKIA passenger flights and calculate delay minutes.

The script reads HKIA's historical JSON endpoint for passenger arrivals and
passenger departures, keeps flights connected with the United Kingdom, Japan,
or South Korea, and writes a CSV file.

Delay is calculated as:

    actual_time - scheduled_time

Positive values mean the flight was delayed; negative values mean early. HKIA's
JSON exposes the scheduled time in the ``time`` field and the latest operation
status in ``status``. When the status contains a clock time, such as
``Landed 18:05`` or ``Departed 09:42``, that time is used as the actual time.
If no time can be found in the status, ``actual_time`` and ``delay_minutes`` are
left blank in the CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HKIA_PAST_FLIGHTS_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights/past"
DEFAULT_OUTPUT = Path("data/hkia_yesterday_uk_japan_korea_flights.csv")
TIME_RE = re.compile(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)")
DATE_RE = re.compile(r"\((\d{1,2})/(\d{1,2})/(\d{4})\)")

# Union of option A (country/territory names and airport/city names) and option B
# (keyword matching). The HKIA API normally returns city/port names rather than a
# normalized country field, so keep this list intentionally broad but specific to
# UK, Japan, and South Korea passenger markets.
REGION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "UK": (
        "united kingdom",
        "great britain",
        " britain",
        " uk",
        "london",
        "heathrow",
        "gatwick",
        "manchester",
        "birmingham",
        "edinburgh",
        "glasgow",
        "newcastle",
        "bristol",
        "liverpool",
        "leeds",
        "stansted",
        "luton",
        "aberdeen",
        "belfast",
        "cardiff",
        "southampton",
        "lhr",
        "lgw",
        "man",
        "bhx",
        "edi",
        "gla",
    ),
    "Japan": (
        "japan",
        "tokyo",
        "narita",
        "haneda",
        "osaka",
        "kansai",
        "nagoya",
        "chubu",
        "fukuoka",
        "sapporo",
        "chitose",
        "okinawa",
        "naha",
        "hiroshima",
        "sendai",
        "kumamoto",
        "kagoshima",
        "miyazaki",
        "takamatsu",
        "ishigaki",
        "komatsu",
        "okayama",
        "shizuoka",
        "nrt",
        "hnd",
        "kix",
        "ngo",
        "fuk",
        "cts",
        "oka",
    ),
    "South Korea": (
        "south korea",
        "korea, republic",
        "republic of korea",
        "seoul",
        "incheon",
        "busan",
        "pusan",
        "gimhae",
        "jeju",
        "daegu",
        "taegu",
        "cheongju",
        "muan",
        "gwangju",
        "icn",
        "gmp",
        "pus",
        "cju",
        "tae",
    ),
}

FIELDNAMES = [
    "query_date",
    "flight_type",
    "region",
    "route_point",
    "scheduled_time",
    "actual_time",
    "delay_minutes",
    "status",
    "status_code",
    "flight_numbers",
    "airline_codes",
    "terminal",
    "gate",
    "stand",
    "hall",
    "baggage_belt",
    "check_in",
    "transfer_desk",
]


def parse_args() -> argparse.Namespace:
    yesterday = date.today() - timedelta(days=1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=yesterday.isoformat(),
        help="HKIA target date in YYYY-MM-DD format. Defaults to yesterday.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"CSV output path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds. Defaults to 30.",
    )
    return parser.parse_args()


def fetch_flight_blocks(query_date: str, *, is_arrival: bool, timeout: float) -> list[dict[str, Any]]:
    params = {
        "date": query_date,
        "lang": "en",
        "cargo": "false",
        "arrival": str(is_arrival).lower(),
    }
    url = f"{HKIA_PAST_FLIGHTS_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "HKAiportSchedule/1.0"})

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HKIA request failed with HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"HKIA request failed: {exc.reason} ({url})") from exc

    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list from HKIA, got {type(data).__name__}")
    return data


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def joined_text(value: Any) -> str:
    return "; ".join(str(item).strip() for item in as_list(value) if str(item).strip())


def extract_flight_numbers(flight: dict[str, Any]) -> str:
    numbers: list[str] = []
    for item in as_list(flight.get("flight")):
        if isinstance(item, dict) and item.get("no"):
            numbers.append(str(item["no"]).strip())
    return "; ".join(numbers)


def extract_airline_codes(flight: dict[str, Any]) -> str:
    codes: list[str] = []
    for item in as_list(flight.get("flight")):
        if isinstance(item, dict) and item.get("airline"):
            codes.append(str(item["airline"]).strip())
    return "; ".join(dict.fromkeys(codes))


def first_joined(flight: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = joined_text(flight.get(key))
        if text:
            return text
    return ""


def match_region(route_point: str) -> str | None:
    normalized = f" {route_point.lower().replace('/', ' ')} "
    matched_regions = []
    for region, keywords in REGION_KEYWORDS.items():
        for keyword in keywords:
            keyword_normalized = f" {keyword.lower().strip()} "
            if keyword_normalized in normalized:
                matched_regions.append(region)
                break
    return "; ".join(matched_regions) if matched_regions else None


def parse_hkia_datetime(query_date: str, time_text: str) -> datetime | None:
    if not time_text:
        return None
    match = TIME_RE.search(time_text)
    if not match:
        return None
    return datetime.strptime(f"{query_date} {match.group(0)}", "%Y-%m-%d %H:%M")


def actual_datetime(query_date: str, scheduled: datetime | None, status: str) -> datetime | None:
    actual_date = query_date
    date_match = DATE_RE.search(status or "")
    if date_match:
        day, month, year = date_match.groups()
        actual_date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    actual = parse_hkia_datetime(actual_date, status)
    if actual is None or scheduled is None:
        return actual

    # Correct common date rollover cases around midnight.
    if actual < scheduled and scheduled.hour >= 18 and actual.hour <= 6:
        actual += timedelta(days=1)
    elif actual - scheduled > timedelta(hours=12):
        actual -= timedelta(days=1)
    return actual


def flight_rows(query_date: str, blocks: list[dict[str, Any]], *, is_arrival: bool) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    flight_type = "arrival" if is_arrival else "departure"
    route_key = "origin" if is_arrival else "destination"

    for block in blocks:
        block_date = str(block.get("date") or query_date)
        for flight in as_list(block.get("list")):
            if not isinstance(flight, dict):
                continue

            route_point = joined_text(flight.get(route_key))
            region = match_region(route_point)
            if region is None:
                continue

            scheduled = parse_hkia_datetime(block_date, str(flight.get("time") or ""))
            status = str(flight.get("status") or "")
            actual = actual_datetime(block_date, scheduled, status)

            delay_minutes = ""
            if scheduled is not None and actual is not None:
                delay_minutes = str(round((actual - scheduled).total_seconds() / 60))

            rows.append(
                {
                    "query_date": block_date,
                    "flight_type": flight_type,
                    "region": region,
                    "route_point": route_point,
                    "scheduled_time": scheduled.isoformat(sep=" ", timespec="minutes") if scheduled else "",
                    "actual_time": actual.isoformat(sep=" ", timespec="minutes") if actual else "",
                    "delay_minutes": delay_minutes,
                    "status": status,
                    "status_code": str(flight.get("statusCode") or ""),
                    "flight_numbers": extract_flight_numbers(flight),
                    "airline_codes": extract_airline_codes(flight),
                    "terminal": first_joined(flight, "terminal"),
                    "gate": first_joined(flight, "gate", "boardingGate"),
                    "stand": first_joined(flight, "stand"),
                    "hall": first_joined(flight, "hall"),
                    "baggage_belt": first_joined(flight, "baggage", "baggageBelt", "belt"),
                    "check_in": first_joined(flight, "checkIn", "checkin", "aisle"),
                    "transfer_desk": first_joined(flight, "transferDesk", "transfer"),
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    # Validate date early to avoid sending malformed requests.
    datetime.strptime(args.date, "%Y-%m-%d")

    rows: list[dict[str, str]] = []
    for is_arrival in (True, False):
        blocks = fetch_flight_blocks(args.date, is_arrival=is_arrival, timeout=args.timeout)
        rows.extend(flight_rows(args.date, blocks, is_arrival=is_arrival))

    rows.sort(key=lambda row: (row["scheduled_time"], row["flight_type"], row["flight_numbers"]))
    output = Path(args.output)
    write_csv(output, rows)
    print(f"Wrote {len(rows)} rows to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
