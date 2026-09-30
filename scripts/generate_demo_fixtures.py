#!/usr/bin/env python3
"""Generate deterministic, synthetic AutoML fixtures for the City Services Copilot demo."""
from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RANDOM = random.Random(3112025)

SERVICES = {
    "Illegal dumping": (4, 0.34, 52),
    "Pothole": (4, 0.23, 68),
    "Missed trash collection": (3, 0.17, 45),
    "Streetlight outage": (6, 0.39, 31),
    "Graffiti": (5, 0.28, 27),
}
NEIGHBORHOODS = ("Kensington", "Center City", "West Philadelphia", "South Philadelphia")
CHANNELS = ("Mobile app", "Phone", "Web")
PRIORITIES = ("Low", "Standard", "High")


def generate_risk_fixture(path: Path, records: int = 5_000) -> None:
    rows = []
    start = datetime(2023, 1, 1, 8, 0)
    for index in range(records):
        service = RANDOM.choice(tuple(SERVICES))
        neighborhood = RANDOM.choice(NEIGHBORHOODS)
        channel = RANDOM.choice(CHANNELS)
        priority = RANDOM.choices(PRIORITIES, weights=(.15, .68, .17))[0]
        target_days, base_risk, _ = SERVICES[service]
        risk = base_risk
        risk += {"Low": -.10, "Standard": 0, "High": .16}[priority]
        risk += {"Mobile app": -.02, "Phone": .04, "Web": .01}[channel]
        risk += {"Kensington": .06, "Center City": -.03, "West Philadelphia": .02, "South Philadelphia": 0}[neighborhood]
        opened = start + timedelta(hours=index * 7 + RANDOM.randrange(6))
        resolution = max(.5, target_days * (0.64 + risk * 1.28) + RANDOM.gauss(0, .75))
        rows.append({
            "service_type": service,
            "neighborhood": neighborhood,
            "intake_channel": channel,
            "priority": priority,
            "opened_at": opened.isoformat(timespec="minutes"),
            "sla_days": target_days,
            "sla_miss": int(resolution > target_days),
        })
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def generate_forecast_fixture(path: Path, days: int = 365) -> None:
    rows = []
    start = date(2024, 1, 1)
    series = tuple(
        (f"{service.lower().replace(' ', '-')}__{neighborhood.lower().replace(' ', '-')}", service, neighborhood)
        for service in SERVICES
        for neighborhood in NEIGHBORHOODS
    )
    for item_id, service, neighborhood in series:
        _, _, baseline = SERVICES[service]
        for offset in range(days):
            current = start + timedelta(days=offset)
            weekend_adjustment = -12 if current.weekday() >= 5 else 0
            seasonal = 8 * math.sin((offset / 365) * 6.283 + baseline / 10)
            trend = offset * .012
            count = max(0, round(baseline + seasonal + weekend_adjustment + trend + RANDOM.gauss(0, 3)))
            rows.append({"timestamp": current.isoformat(), "item_id": item_id, "target": count, "service_type": service, "neighborhood": neighborhood})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    prepared = ROOT / "data" / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    generate_risk_fixture(prepared / "resolution-risk.csv")
    generate_forecast_fixture(prepared / "service-demand-daily.csv")
    print("Generated 5,000 tabular rows and 7,300 time-series rows in data/prepared/.")


if __name__ == "__main__":
    main()
