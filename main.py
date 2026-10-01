"""City Services Copilot — self-contained AutoML + AutoRAG demo.

Run with: python3 main.py, then open http://localhost:8000.
Optionally add app-config.json to call deployed scoring and AutoRAG endpoints.
"""
from __future__ import annotations

import csv, html, json, math, re, ssl
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
    for key in ("endpoint_tls_verify", "endpoint_ca_bundles"):
        if key in config and not isinstance(config[key], dict):
            raise RuntimeError(f"app-config.json {key} must be an object")
    if "tabular_feature_importance" in config and not isinstance(config["tabular_feature_importance"], dict):
        raise RuntimeError("app-config.json tabular_feature_importance must be an object")
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
        verify_tls = APP_CONFIG.get("endpoint_tls_verify", {}).get(name, True)
        ca_bundle = APP_CONFIG.get("endpoint_ca_bundles", {}).get(name)
        context = ssl.create_default_context(cafile=ca_bundle) if verify_tls else ssl._create_unverified_context()
        with urlopen(request, context=context, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise EndpointError(f"{name} endpoint returned HTTP {error.code}") from error
    except URLError as error:
        reason = str(error.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            raise EndpointError(
                f"{name} endpoint TLS certificate is not trusted; configure APP_ENDPOINT_CA_BUNDLE "
                "or disable verification only for trusted development"
            ) from error
        raise EndpointError(f"{name} endpoint is unreachable: {reason}") from error
    except TimeoutError as error:
        raise EndpointError(f"{name} endpoint timed out") from error
    except json.JSONDecodeError as error:
        raise EndpointError(f"{name} endpoint returned invalid JSON") from error


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
    row = {
        "service_type": service,
        "neighborhood": p.get("neighborhood", "Kensington"),
        "intake_channel": p.get("channel", "Mobile app"),
        "priority": p.get("priority", "Standard"),
        "opened_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "sla_days": round(days),
    }
    # The AutoGluon KServe v1 server consumes one list-wrapped value per field
    # in each instance; scalar values cannot be converted into a DataFrame.
    return {"instances": [{key: [value] for key, value in row.items()}]}


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


def global_feature_importance():
    """Adapt the selected AutoML model's persisted permutation importance for the UI."""
    metadata = APP_CONFIG.get("tabular_feature_importance", {})
    rows = metadata.get("features", []) if isinstance(metadata, dict) else []
    labels = {
        "service_type": "Service type",
        "neighborhood": "Neighborhood",
        "intake_channel": "Intake channel",
        "priority": "Priority",
        "opened_at": "Request time",
        "sla_days": "SLA days",
    }
    drivers = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        try:
            score = float(row["importance"])
        except (KeyError, TypeError, ValueError):
            continue
        drivers.append([labels.get(row["name"], row["name"].replace("_", " ").title()), f"{score:.3f}", score])
    return drivers[:6]


def kserve_tabular_result(response, p):
    prediction = first_prediction(response)
    positive = prediction is True or prediction == 1 or str(prediction).strip().lower() in {"1", "true", "yes"}
    base_days = SERVICE_BASELINES.get(p.get("service"), SERVICE_BASELINES["Illegal dumping"])[0]
    risk = 74 if positive else 22
    return {
        "risk": risk,
        "days": round(base_days * (1.35 if positive else .8), 1),
        "band": "At risk of SLA miss" if positive else "Likely within SLA",
        "drivers": global_feature_importance(),
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
    return {"risk":round(risk*100),"days":round(resolution,1),"band":"Likely within SLA" if risk<.35 else "Needs attention" if risk<.55 else "At risk of SLA miss","drivers":[]}

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
    answer=("For an illegal-dumping report, confirm the closest address, a clear description, and safe-to-share photos. Route the request for inspection, cleanup, or enforcement based on field findings, and advise the resident to track its status through 311. Escalate an immediate hazard to 911." if any(x in question for x in ("dump","trash","illegal")) else "Confirm the location and description, route the request to the responsible city team, and give the resident the 311 status-tracking guidance. Escalate an immediate hazard to 911.")
    return {"answer":answer,"sources":sources}


def markdown_inline(text):
    """Render the small, trusted README Markdown subset without an extra dependency."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^ )]+)(?: \"[^\"]*\")?\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def markdown_guide(markdown):
    """Turn README.md into a lightweight, readable local setup-guide page."""
    output, paragraph, list_kind = [], [], None

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            output.append(f"<p>{markdown_inline(' '.join(part.strip() for part in paragraph))}</p>")
            paragraph = []

    def close_list():
        nonlocal list_kind
        if list_kind:
            output.append(f"</{list_kind}>")
            list_kind = None

    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush_paragraph(); close_list()
            language = line[3:].strip()
            index += 1
            block = []
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index]); index += 1
            output.append(f'<pre><code class="language-{html.escape(language)}">{html.escape(chr(10).join(block))}</code></pre>')
        elif not line.strip():
            flush_paragraph(); close_list()
        elif match := re.match(r"^(#{1,4})\s+(.+)$", line):
            flush_paragraph(); close_list()
            level = len(match.group(1))
            output.append(f"<h{level}>{markdown_inline(match.group(2))}</h{level}>")
        elif line.startswith("> "):
            flush_paragraph(); close_list()
            output.append(f"<blockquote>{markdown_inline(line[2:])}</blockquote>")
        elif re.match(r"^\|?\s*[-:]+(?:\s*\|\s*[-:]+)+\s*\|?$", line):
            index += 1
            continue
        elif line.startswith("|") and "|" in line[1:]:
            flush_paragraph(); close_list()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not output or not output[-1].startswith("<table"):
                output.append("<table><tbody>")
            output.append("<tr>" + "".join(f"<td>{markdown_inline(cell)}</td>" for cell in cells) + "</tr>")
            if index + 1 == len(lines) or not lines[index + 1].startswith("|"):
                output.append("</tbody></table>")
        elif match := re.match(r"^[-*+]\s+(.+)$", line):
            flush_paragraph()
            if list_kind != "ul":
                close_list(); output.append("<ul>"); list_kind = "ul"
            output.append(f"<li>{markdown_inline(match.group(1))}</li>")
        elif match := re.match(r"^\d+\.\s+(.+)$", line):
            flush_paragraph()
            if list_kind != "ol":
                close_list(); output.append("<ol>"); list_kind = "ol"
            output.append(f"<li>{markdown_inline(match.group(1))}</li>")
        elif line.startswith("!["):
            flush_paragraph(); close_list()
            match = re.match(r"!\[([^\]]*)\]\(([^ )]+)\)", line)
            if match:
                output.append(f'<img src="{html.escape(match.group(2), quote=True)}" alt="{html.escape(match.group(1), quote=True)}">')
        else:
            paragraph.append(line)
        index += 1
    flush_paragraph(); close_list()
    return "\n".join(output)


def setup_guide_html():
    guide = markdown_guide((ROOT / "README.md").read_text(encoding="utf-8"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>City Services Copilot — Setup guide</title>
<style>
body{{margin:0;background:#f2f5ef;color:#14221e;font:16px/1.65 system-ui,sans-serif}}main{{max-width:900px;margin:0 auto;padding:38px 30px 70px;background:#fff;min-height:100vh}}a{{color:#175b45}}h1,h2,h3,h4{{line-height:1.2;margin-top:2em}}h1{{margin-top:0;font-size:2.35rem}}h2{{border-bottom:1px solid #d9dfdb;padding-bottom:.35em}}pre{{overflow:auto;padding:16px;background:#1c2d28;color:#fff;border-radius:5px}}code{{background:#edf2ef;padding:.1em .3em;border-radius:3px}}pre code{{padding:0;background:transparent}}table{{width:100%;border-collapse:collapse;margin:1.25em 0}}td{{border:1px solid #d9dfdb;padding:.55em;vertical-align:top}}blockquote{{margin:1em 0;padding:.5em 1em;border-left:4px solid #caf172;background:#f2f5ef}}img{{max-width:100%;border:1px solid #d9dfdb;border-radius:5px}}.back{{display:inline-block;margin-bottom:2em;font-weight:700;text-decoration:none}}
</style></head><body><main><a class="back" href="/">← Back to the demo</a>{guide}</main></body></html>"""


def demo_page_html():
    """Read the checked-in, operator-facing home page without browser caching."""
    return (ROOT / "index.html").read_text(encoding="utf-8")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=str(ROOT),**k)
    def log_message(self,*a): return
    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser cancelled or replaced the request while the endpoint was responding.
            pass
    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/index.html", "/setup", "/README.md"}:
            body = (setup_guide_html() if path in {"/setup", "/README.md"} else demo_page_html()).encode("utf-8")
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        super().do_GET()
    def do_POST(self):
        try:
            p=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            route=urlparse(self.path).path
            result={"/api/predict":predict_request,"/api/forecast":forecast_volume,"/api/ask":retrieve_guidance}.get(route)
            if not result: self.send_error(HTTPStatus.NOT_FOUND); return
            self.send_json(HTTPStatus.OK, result(p))
        except EndpointError as e: self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
        except (ValueError,json.JSONDecodeError) as e: self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

if __name__ == "__main__":
    print("City Services Copilot running at http://localhost:8000")
    live_endpoints = [name for name in ("tabular_scoring", "timeseries_scoring") if APP_CONFIG.get("endpoints", {}).get(name)]
    if live_endpoints:
        print("Live AutoML scoring enabled: " + ", ".join(live_endpoints))
    else:
        print("Using bundled sample scoring results.")
    if not APP_CONFIG.get("endpoints", {}).get("responses"):
        print("Guidance answers are using the bundled sample corpus (AutoRAG endpoint not configured).")
    ThreadingHTTPServer(("127.0.0.1",8000),Handler).serve_forever()
