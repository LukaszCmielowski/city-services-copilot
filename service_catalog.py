"""Service facts shared by fixture generation and live AutoML scoring.

sla_days is both the training label threshold and the value sent at score time.
"""
import json
from pathlib import Path

SERVICES = {
    "Illegal dumping": {"sla_days": 4, "base_risk": 0.34, "daily_volume": 52, "guidance_file": "illegal-dumping.md"},
    "Pothole": {"sla_days": 4, "base_risk": 0.23, "daily_volume": 68, "guidance_file": "pothole-repair.md"},
    "Missed trash collection": {"sla_days": 3, "base_risk": 0.17, "daily_volume": 45, "guidance_file": "missed-trash-collection.md"},
    "Streetlight outage": {"sla_days": 6, "base_risk": 0.39, "daily_volume": 31, "guidance_file": "streetlight-outage.md"},
    "Graffiti": {"sla_days": 5, "base_risk": 0.28, "daily_volume": 27, "guidance_file": "graffiti-removal.md"},
}
DEFAULT_SERVICE = "Illegal dumping"
SAMPLE_REQUESTS = json.loads((Path(__file__).parent / "data" / "sample-requests.json").read_text())
PRIORITY_RISK = {"Low": -.10, "Standard": 0, "High": .16}
CHANNEL_RISK = {"Mobile app": -.02, "Phone": .04, "Web": .01}
NEIGHBORHOOD_RISK = {"Kensington": .06, "Center City": -.03, "West Philadelphia": .02, "South Philadelphia": 0}


def sla_days_for(service: str) -> int:
    """Return the trained SLA for a service, falling back to the default service."""
    record = SERVICES.get(service) or SERVICES[DEFAULT_SERVICE]
    return record["sla_days"]
