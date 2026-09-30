"""City Services Copilot — self-contained AutoML + AutoRAG demo.

Run with: python3 main.py, then open http://localhost:8000.
Optionally add app-config.json to call deployed scoring and AutoRAG endpoints.
"""
from __future__ import annotations

import csv, json, math, re
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "app-config.json"


class EndpointError(Exception):
    """A configured inference endpoint could not process a request."""


def load_config():
    """Load optional server-only endpoint configuration from the repository root."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError("app-config.json is not valid JSON") from error
    if not isinstance(config.get("api_token"), str) or not isinstance(config.get("endpoints"), dict):
        raise RuntimeError("app-config.json requires api_token and endpoints")
    if "endpoint_tokens" in config and not isinstance(config["endpoint_tokens"], dict):
        raise RuntimeError("app-config.json endpoint_tokens must be an object")
    return config


APP_CONFIG = load_config()


def call_endpoint(name, payload):
    """Forward JSON to a configured endpoint without exposing its token to the UI."""
    endpoint = APP_CONFIG.get("endpoints", {}).get(name)
    if not endpoint:
        return None
    token = APP_CONFIG.get("endpoint_tokens", {}).get(name, APP_CONFIG["api_token"])
    if not isinstance(token, str) or not token:
        raise EndpointError(f"{name} endpoint has no API token")
    body = json.dumps(payload).encode("utf-8")
    request = Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise EndpointError(f"{name} endpoint request failed") from error


def uses_kserve_v1(name):
    return APP_CONFIG.get("endpoint_protocol") == "kserve-v1" and bool(APP_CONFIG.get("endpoints", {}).get(name))


def responses_result(response):
    """Adapt an OpenAI Responses-compatible RAG reply to the UI contract."""
    answer = response.get("output_text", "")
    sources = []
    for output in response.get("output", []):
        for content in output.get("content", []):
            if content.get("type") != "output_text":
                continue
            answer = answer or content.get("text", "")
            for annotation in content.get("annotations", []):
                if annotation.get("type") == "url_citation" and annotation.get("url"):
                    sources.append({
                        "title": annotation.get("title", annotation["url"]),
                        "agency": "Source",
                        "url": annotation["url"],
                    })
    if not answer:
        raise EndpointError("responses endpoint returned no output text")
    return {"answer": answer, "sources": sources}


SERVICE_BASELINES = {
    "Illegal dumping": (4.2, .31, 52), "Pothole": (3.5, .22, 68),
    "Missed trash collection": (2.1, .15, 45), "Streetlight outage": (6.8, .38, 31),
    "Graffiti": (5.1, .28, 27),
}


def load_forecast_history():
    path = ROOT / "data" / "prepared" / "service-demand-daily.csv"
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


FORECAST_HISTORY = load_forecast_history()


def kserve_tabular_payload(p):
    service = p.get("service", "Illegal dumping")
    days = SERVICE_BASELINES.get(service, SERVICE_BASELINES["Illegal dumping"])[0]
    return {"instances": [{
        "service_type": service,
        "neighborhood": p.get("neighborhood", "Kensington"),
        "intake_channel": p.get("channel", "Mobile app"),
        "priority": p.get("priority", "Standard"),
        "opened_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "sla_days": round(days),
    }]}


def first_prediction(response):
    prediction = response.get("predictions") if isinstance(response, dict) else None
    if isinstance(prediction, list) and prediction:
        prediction = prediction[0]
    if isinstance(prediction, list) and prediction:
        prediction = prediction[0]
    if isinstance(prediction, dict):
        prediction = prediction.get("prediction", prediction.get("label", prediction.get("value")))
    if prediction is None:
        raise EndpointError("KServe endpoint returned no predictions")
    return prediction


def kserve_tabular_result(response, p):
    prediction = first_prediction(response)
    positive = prediction is True or prediction == 1 or str(prediction).strip().lower() in {"1", "true", "yes"}
    base_days = SERVICE_BASELINES.get(p.get("service"), SERVICE_BASELINES["Illegal dumping"])[0]
    risk = 74 if positive else 22
    return {
        "risk": risk,
        "days": round(base_days * (1.35 if positive else .8), 1),
        "band": "At risk of SLA miss" if positive else "Likely within SLA",
        "drivers": [["AutoML predicted class", "SLA miss" if positive else "Within SLA", 100]],
    }


def kserve_timeseries_payload(p):
    service = p.get("service", "Illegal dumping")
    neighborhood = p.get("neighborhood", "Kensington")
    item_id = f"{service.lower().replace(' ', '-')}__{neighborhood.lower().replace(' ', '-')}"
    rows = [row for row in FORECAST_HISTORY if row["item_id"] == item_id][-35:]
    if not rows:
        raise EndpointError(f"No bundled forecast history is available for {service} in {neighborhood}")
    return {"instances": [{key: (float(value) if key == "target" else value) for key, value in row.items()} for row in rows]}


def kserve_forecast_result(response):
    values = response.get("predictions") if isinstance(response, dict) else None
    if not isinstance(values, list) or not values:
        raise EndpointError("KServe time-series endpoint returned no predictions")
    parsed = []
    pending = list(values)
    while pending:
        value = pending.pop(0)
        if isinstance(value, list):
            pending[0:0] = value
            continue
        if isinstance(value, dict):
            value = value.get("target", value.get("mean", value.get("prediction")))
        try:
            parsed.append(round(float(value)))
        except (TypeError, ValueError) as error:
            raise EndpointError("KServe time-series endpoint returned an unsupported prediction") from error
    if len(parsed) < 7:
        raise EndpointError("KServe time-series endpoint returned fewer than seven forecast points")
    start = date.today() + timedelta(days=1)
    points = [{"label": (start + timedelta(days=index)).strftime("%a"), "value": value} for index, value in enumerate(parsed[:7])]
    return {"points": points, "total": sum(parsed[:7]), "baseline": round(sum(parsed[:7]) / 7)}

def load_guidance():
    """Load the same bundled Markdown corpus that the AutoRAG setup uploads."""
    documents = []
    for path in sorted((ROOT / "data" / "guidance").glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        source_match = re.search(r"^Source:\s*(https?://\S+)$", content, re.MULTILINE)
        text = re.sub(r"^#\s+.+$|^Source:\s*https?://\S+$", "", content, flags=re.MULTILINE).strip()
        documents.append({
            "title": title_match.group(1) if title_match else path.stem.replace("-", " ").title(),
            "agency": "City of Philadelphia",
            "url": source_match.group(1) if source_match else "https://www.phila.gov/311/",
            "text": text,
        })
    return documents

GUIDANCE = load_guidance()

def predict_request(p):
    payload = kserve_tabular_payload(p) if uses_kserve_v1("tabular_scoring") else p
    live = call_endpoint("tabular_scoring", payload)
    if live is not None:
        return kserve_tabular_result(live, p) if uses_kserve_v1("tabular_scoring") else live
    service = p.get("service", "Illegal dumping")
    days, base_risk, _ = SERVICE_BASELINES.get(service, SERVICE_BASELINES["Illegal dumping"])
    risk = base_risk + {"Phone":.04,"Web":.01,"Mobile app":-.02}.get(p.get("channel"), 0) + {"High":.16,"Standard":0,"Low":-.08}.get(p.get("priority"),0) + {"Kensington":.06,"Center City":-.03,"West Philadelphia":.02,"South Philadelphia":0}.get(p.get("neighborhood"),0)
    risk=max(.04,min(.86,risk)); resolution=max(1, days*(.83+risk*.58))
    return {"risk":round(risk*100),"days":round(resolution,1),"band":"Likely within SLA" if risk<.35 else "Needs attention" if risk<.55 else "At risk of SLA miss","drivers":[["Service type",service,42],["Priority",p.get("priority","Standard"),27],["Neighborhood",p.get("neighborhood","Kensington"),18],["Intake channel",p.get("channel","Mobile app"),13]]}

def forecast_volume(p):
    payload = kserve_timeseries_payload(p) if uses_kserve_v1("timeseries_scoring") else p
    live = call_endpoint("timeseries_scoring", payload)
    if live is not None:
        return kserve_forecast_result(live) if uses_kserve_v1("timeseries_scoring") else live
    _, _, base = SERVICE_BASELINES.get(p.get("service"), SERVICE_BASELINES["Illegal dumping"]); start=date.today()+timedelta(days=1); points=[]
    for i in range(7):
        d=start+timedelta(days=i); val=round(max(12,base+7*math.sin((i+1)*1.4)+(-13 if d.weekday()>4 else 0)+i*.8)); points.append({"label":d.strftime("%a"),"value":val})
    return {"points":points,"total":sum(x["value"] for x in points),"baseline":base}

def retrieve_guidance(p):
    live = call_endpoint("responses", {"input": p.get("question", "")})
    if live is not None:
        return responses_result(live)
    question=p.get("question","").lower(); tokens=set(re.findall(r"[a-z]{3,}",question))
    sources=sorted(GUIDANCE,key=lambda x:len(tokens & set(re.findall(r"[a-z]{3,}",(x["title"]+" "+x["text"]).lower()))),reverse=True)[:2]
    answer=("For this report, submit the closest address, a clear description, and photos if it is safe. The request is routed for inspection, cleanup, or enforcement depending on what crews find. You can follow its status through 311; use 911 for an immediate hazard." if any(x in question for x in ("dump","trash","illegal")) else "311 can route this request to the responsible city team and provide status updates. Include a precise location and description; immediate hazards should go to 911.")
    return {"answer":answer,"sources":sources}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=str(ROOT),**k)
    def log_message(self,*a): return
    def do_POST(self):
        try:
            p=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            route=urlparse(self.path).path
            result={"/api/predict":predict_request,"/api/forecast":forecast_volume,"/api/ask":retrieve_guidance}.get(route)
            if not result: self.send_error(HTTPStatus.NOT_FOUND); return
            body=json.dumps(result(p)).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        except EndpointError as e: self.send_error(HTTPStatus.BAD_GATEWAY, str(e))
        except (ValueError,json.JSONDecodeError) as e: self.send_error(HTTPStatus.BAD_REQUEST,str(e))

if __name__ == "__main__":
    print("City Services Copilot running at http://localhost:8000")
    ThreadingHTTPServer(("127.0.0.1",8000),Handler).serve_forever()
