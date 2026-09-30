"""City Services Copilot — self-contained AutoML + AutoRAG demo.

Run with: python3 main.py, then open http://localhost:8000.
Optionally add app-config.json to call deployed scoring and AutoRAG endpoints.
"""
from __future__ import annotations

import json, math, re
from datetime import date, timedelta
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
    return config


APP_CONFIG = load_config()


def call_endpoint(name, payload):
    """Forward JSON to a configured endpoint without exposing its token to the UI."""
    endpoint = APP_CONFIG.get("endpoints", {}).get(name)
    if not endpoint:
        return None
    body = json.dumps(payload).encode("utf-8")
    request = Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {APP_CONFIG['api_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise EndpointError(f"{name} endpoint request failed") from error


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
    live = call_endpoint("tabular_scoring", p)
    if live is not None:
        return live
    service = p.get("service", "Illegal dumping")
    days, base_risk, _ = SERVICE_BASELINES.get(service, SERVICE_BASELINES["Illegal dumping"])
    risk = base_risk + {"Phone":.04,"Web":.01,"Mobile app":-.02}.get(p.get("channel"), 0) + {"High":.16,"Standard":0,"Low":-.08}.get(p.get("priority"),0) + {"Kensington":.06,"Center City":-.03,"West Philadelphia":.02,"South Philadelphia":0}.get(p.get("neighborhood"),0)
    risk=max(.04,min(.86,risk)); resolution=max(1, days*(.83+risk*.58))
    return {"risk":round(risk*100),"days":round(resolution,1),"band":"Likely within SLA" if risk<.35 else "Needs attention" if risk<.55 else "At risk of SLA miss","drivers":[["Service type",service,42],["Priority",p.get("priority","Standard"),27],["Neighborhood",p.get("neighborhood","Kensington"),18],["Intake channel",p.get("channel","Mobile app"),13]]}

def forecast_volume(p):
    live = call_endpoint("timeseries_scoring", p)
    if live is not None:
        return live
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
