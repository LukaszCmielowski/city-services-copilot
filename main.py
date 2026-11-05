"""City Services Copilot — self-contained AutoML + AutoRAG demo.

Run with: python3 main.py, then open http://localhost:8000.
Optionally add app-config.json to call deployed scoring and AutoRAG endpoints.
"""
from __future__ import annotations

import csv, html, json, math, re, ssl, sys
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from service_catalog import SERVICES, DEFAULT_SERVICE, SAMPLE_REQUESTS, PRIORITY_RISK, CHANNEL_RISK, NEIGHBORHOOD_RISK, sla_days_for

CONFIG_PATH = ROOT / "app-config.json"
PUBLIC_STATIC_PATHS = {"/app.js", "/styles.css", "/docs/assets/sample-ui.png"}


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
    for key in ("endpoint_tls_verify", "endpoint_ca_bundles", "endpoint_tls_strict"):
        if key in config and not isinstance(config[key], dict):
            raise RuntimeError(f"app-config.json {key} must be an object")
    if "tabular_feature_importance" in config and not isinstance(config["tabular_feature_importance"], dict):
        raise RuntimeError("app-config.json tabular_feature_importance must be an object")
    if config.get("responses_auth_header", "Authorization") not in {"Authorization", "X-Api-Key"}:
        raise RuntimeError("responses_auth_header must be Authorization or X-Api-Key")
    if config.get("responses_protocol", "responses") != "responses" or config.get("endpoint_protocol", "kserve-v1") != "kserve-v1":
        raise RuntimeError("Use KServe scoring and the deployed starter-kit Responses API")
    return config


APP_CONFIG = load_config()


def open_endpoint(name, payload, timeout=30):
    """Open a configured endpoint without exposing its token to the UI."""
    endpoint = APP_CONFIG.get("endpoints", {}).get(name)
    if not endpoint:
        return None
    token = APP_CONFIG.get("endpoint_tokens", {}).get(name, APP_CONFIG["api_token"])
    if not isinstance(token, str) or not token:
        raise EndpointError(f"{name} endpoint has no API token")
    body = json.dumps(payload).encode("utf-8")
    auth_header = APP_CONFIG.get("responses_auth_header", "Authorization") if name == "responses" else "Authorization"
    request = Request(endpoint, data=body, method="POST", headers={
        auth_header: token if auth_header == "X-Api-Key" else f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        verify_tls = APP_CONFIG.get("endpoint_tls_verify", {}).get(name, True)
        ca_bundle = APP_CONFIG.get("endpoint_ca_bundles", {}).get(name)
        context = ssl.create_default_context(cafile=ca_bundle) if verify_tls else ssl._create_unverified_context()
        if APP_CONFIG.get("endpoint_tls_strict", {}).get(name, True) is False:
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return urlopen(request, context=context, timeout=timeout)
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


def call_endpoint(name, payload, timeout=30):
    """Forward JSON to a configured endpoint and return the parsed body."""
    response = open_endpoint(name, payload, timeout=timeout)
    if response is None:
        return None
    try:
        with response:
            return json.loads(response.read())
    except json.JSONDecodeError as error:
        raise EndpointError(f"{name} endpoint returned invalid JSON") from error
    except (TimeoutError, URLError) as error:
        raise EndpointError(f"{name} endpoint response could not be read") from error


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
    grounding = "citations" if sources else "none"
    return {"answer": answer, "sources": sources, "grounding": grounding}


def load_forecast_history():
    path = ROOT / "data" / "prepared" / "service-demand-daily.csv"
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


FORECAST_HISTORY = load_forecast_history()


def kserve_tabular_payload(p):
    service = p.get("service", "Illegal dumping")
    row = {
        "service_type": service,
        "neighborhood": p.get("neighborhood", "Kensington"),
        "intake_channel": p.get("channel", "Mobile app"),
        "priority": p.get("priority", "Standard"),
        "opened_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "sla_days": sla_days_for(service),
    }
    # The AutoGluon KServe v1 server consumes one list-wrapped value per field
    # in each instance; scalar values cannot be converted into a DataFrame.
    return {"instances": [{key: [value] for key, value in row.items()}]}


def probability_value(value, *, explicit=False):
    """Validate a probability; bare 0/1 predictions remain class labels."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(value) and (0 <= value <= 1 if explicit else 0 < value < 1):
        return value
    return None


def prediction_parts(response):
    """Separate a KServe class label from a probability when one is present."""
    prediction = response.get("predictions") if isinstance(response, dict) else None
    while isinstance(prediction, list):
        if len(prediction) != 1:
            raise EndpointError("KServe endpoint returned an unsupported prediction")
        prediction = prediction[0]
    if prediction is None:
        raise EndpointError("KServe endpoint returned no predictions")
    probability = None
    if isinstance(prediction, dict):
        # AutoGluon v1 predict_proba returns one dictionary per request,
        # keyed by the original target labels: 0=within SLA, 1=SLA miss.
        if set(prediction) == {"0", "1"}:
            within = probability_value(prediction["0"], explicit=True)
            probability = probability_value(prediction["1"], explicit=True)
            if within is None or probability is None or not math.isclose(within + probability, 1, abs_tol=1e-6):
                raise EndpointError("KServe endpoint returned invalid SLA class probabilities")
            return probability >= 0.5, probability
        label = next((prediction[key] for key in ("prediction", "label", "class", "value") if prediction.get(key) is not None), None)
        for key in ("probability", "proba"):
            if key in prediction:
                probability = probability_value(prediction[key], explicit=True)
                if probability is not None:
                    break
        if label is None and probability is None:
            raise EndpointError("KServe endpoint returned no predictions")
        if label is None:
            label = probability >= 0.5
        return label, probability
    probability = probability_value(prediction)
    if probability is not None:
        return probability >= 0.5, probability
    return prediction, None


def positive_class(label):
    if isinstance(label, bool):
        return label
    if isinstance(label, (int, float)) and not isinstance(label, bool) and float(label) == 1.0:
        return True
    return str(label).strip().lower() in {"1", "true", "yes"}


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


def kserve_tabular_result(response):
    label, probability = prediction_parts(response)
    positive = positive_class(label)
    result = {
        "mode": "model",
        "outcome": "sla_miss" if positive else "within_sla",
        "drivers": global_feature_importance(),
    }
    if probability is not None:
        result["risk"] = round(probability * 100)
    return result


def series_rows(p):
    service = p.get("service", "Illegal dumping")
    neighborhood = p.get("neighborhood", "Kensington")
    item_id = f"{service.lower().replace(' ', '-')}__{neighborhood.lower().replace(' ', '-')}"
    rows = [row for row in FORECAST_HISTORY if row["item_id"] == item_id][-35:]
    if not rows:
        raise EndpointError(f"No bundled forecast history is available for {service} in {neighborhood}")
    return rows


def history_instances(rows):
    return {"instances": [
        {key: (float(value) if key == "target" else value) for key, value in row.items()}
        for row in rows
    ]}


def kserve_timeseries_payload(p):
    return history_instances(series_rows(p))


def forecast_result(values, source):
    return {"points": [{"value": value} for value in values], "total": sum(values), "source": source}


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
    return forecast_result(parsed[:7], "model")


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
            "file": path.name,
            "agency": "City of Philadelphia",
            "url": source_match.group(1) if source_match else "https://www.phila.gov/311/",
            "text": text,
        })
    return documents

GUIDANCE = load_guidance()


def guidance_object(result, label):
    """Parse the structured answer requested from the agent."""
    try:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result["answer"].strip())
        content = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise EndpointError(f"AutoRAG returned an invalid {label}; try again") from error
    if not isinstance(content, dict):
        raise EndpointError(f"AutoRAG returned an invalid {label}; try again")
    return content


def guidance_sources(result, urls):
    """Preserve citations and admit generated references only from the bundled corpus."""
    sources = list(result["sources"])
    known = {doc["url"]: doc for doc in GUIDANCE}
    if isinstance(urls, list):
        for url in urls:
            if isinstance(url, str) and url in known and not any(source.get("url") == url for source in sources):
                doc = known[url]
                sources.append({"url": url, "title": doc["title"], "agency": "City of Philadelphia"})
    grounding = result["grounding"]
    if grounding == "none" and sources:
        grounding = "references"
    return {"sources": sources, "grounding": grounding}


def operator_brief(p):
    """Ask AutoRAG for policy guidance; model estimates stay outside the draft."""
    service = p.get("service", "Illegal dumping")
    if service not in SERVICES:
        raise ValueError("Choose a supported service for the handling brief")
    context = guidance_input({**p, "question": ""})
    prompt = (
        f"{context} Intake channel: {p.get('channel', 'Mobile app')}. "
        f"Operator-assigned priority: {p.get('priority', 'Standard')}. "
        "Retrieve the relevant city service guidance. Return only one JSON object with "
        '"handling_steps": an array of 2 to 4 concise operator instructions, '
        '"resident_reply": a short resident-facing response draft, and '
        '"source_urls": an array of the exact Source URLs from the retrieved documents. '
        "Use only retrieved evidence for routing, information to collect, and safety advice. "
        "If the evidence is insufficient, say what requires verification. "
        "Do not invent a department, completed action, request number, appointment, or completion promise. "
        "This is an incoming report, so do not claim it has already been submitted or assigned. "
        "Do not invent numerical risk, forecasts, or resolution times. "
        "Do not treat a predicted SLA miss or assigned priority as an emergency. "
        "Return source_urls as [] if the retrieved evidence has no Source URL."
    )
    result = call_guidance(prompt)
    if result is None:
        document = next((doc for doc in GUIDANCE if doc["file"] == SERVICES[service]["guidance_file"]), None)
        if document is None:
            raise EndpointError("Bundled service guidance is unavailable")
        return {
            "handling_steps": [document["text"]],
            "resident_reply": f"Thank you for reporting {service.lower()}. Please provide a precise location and the details requested in the service guidance, with photos only when safe. You can follow a submitted request through 311. Completion timing depends on review and site conditions.",
            "sources": [document], "grounding": "sample",
        }
    brief = guidance_object(result, "handling brief")
    steps, reply = brief.get("handling_steps"), brief.get("resident_reply")
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 6
            or not all(isinstance(step, str) and step.strip() for step in steps)
            or not isinstance(reply, str) or not reply.strip()):
        raise EndpointError("AutoRAG returned an incomplete handling brief")
    return {"handling_steps": steps, "resident_reply": reply,
            **guidance_sources(result, brief.get("source_urls", []))}


def predict_request(p):
    live = call_endpoint("tabular_scoring", kserve_tabular_payload(p))
    service = SERVICES.get(p.get("service"), SERVICES[DEFAULT_SERVICE])
    if live is not None:
        return {**kserve_tabular_result(live), "sla_days": service["sla_days"]}
    risk = service["base_risk"] + PRIORITY_RISK.get(p.get("priority"), 0) + CHANNEL_RISK.get(p.get("channel"), 0) + NEIGHBORHOOD_RISK.get(p.get("neighborhood"), 0)
    risk = max(.04, min(.86, risk))
    return {"mode": "sample", "risk": round(risk*100), "sla_days": service["sla_days"],
            "outcome": "sla_miss" if risk >= .5 else "within_sla", "drivers": []}


def forecast_volume(p):
    live = call_endpoint("timeseries_scoring", kserve_timeseries_payload(p))
    if live is not None:
        return kserve_forecast_result(live)
    base = SERVICES.get(p.get("service"), SERVICES[DEFAULT_SERVICE])["daily_volume"]
    values = [round(max(0, base + 7*math.sin((i+1)*1.4) + (-13 if i in (4, 5) else 0) + i*.8)) for i in range(7)]
    return forecast_result(values, "sample")


def guidance_input(p):
    """Attach the selected request so the answer is about the case on screen."""
    question = (p.get("question") or "").strip()
    service = (p.get("service") or "").strip()
    neighborhood = (p.get("neighborhood") or "").strip()
    if not (service and neighborhood):
        return question
    context = f"Service request: {service} in {neighborhood}."
    return f"{context} {question}".strip() if question else context

def retrieve_guidance(p):
    question = guidance_input(p)
    prompt = (
        f"{question}\n\n"
        "Retrieve the relevant city service guidance and answer the question using only retrieved evidence. "
        "Return only one JSON object with "
        '\"answer\": a concise answer to the question, and '
        '\"source_urls\": an array of the exact Source URLs from the retrieved documents used for the answer. '
        "Do not invent references or service commitments. If the evidence is insufficient, say what requires verification. "
        "Return source_urls as [] if the retrieved evidence has no Source URL."
    )
    live = call_guidance(prompt)
    if live is not None:
        content = guidance_object(live, "policy answer")
        answer = content.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise EndpointError("AutoRAG returned an incomplete policy answer; try again")
        return {"answer": answer.strip(), **guidance_sources(live, content.get("source_urls", []))}
    tokens=set(re.findall(r"[a-z]{3,}",question.lower()))
    sources=sorted(GUIDANCE,key=lambda x:len(tokens & set(re.findall(r"[a-z]{3,}",(x["title"]+" "+x["text"]).lower()))),reverse=True)[:2]
    service = (p.get("service") or "").strip()
    neighborhood = (p.get("neighborhood") or "").strip()
    subject = f"{service} request in {neighborhood}" if service and neighborhood else "request"
    focus = service.lower() if service else question.lower()
    if any(x in focus for x in ("dump","trash","illegal")):
        answer = f"For this {subject}, confirm the closest address, a clear description, and safe-to-share photos. Route the request for inspection, cleanup, or enforcement based on field findings, and advise the resident to track its status through 311. Escalate an immediate hazard to 911."
    else:
        answer = f"For this {subject}, confirm the location and description, route the request to the responsible city team, and give the resident the 311 status-tracking guidance. Escalate an immediate hazard to 911."
    return {"answer":answer,"sources":sources,"grounding":"sample"}


def call_guidance(question):
    """Both guidance routes use the deployed agent's non-streaming Responses API."""
    live = call_endpoint("responses", {"input": question, "stream": False}, timeout=180)
    return responses_result(live) if live is not None else None


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
            anchor = re.sub(r"[^\w -]", "", match.group(2).lower()).strip().replace(" ", "-")
            output.append(f'<h{level} id="{anchor}">{markdown_inline(match.group(2))}</h{level}>')
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


def demo_mode_label():
    """One chip for the room: sample results, or whichever live path is configured."""
    endpoints = APP_CONFIG.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        endpoints = {}
    scoring = bool(endpoints.get("tabular_scoring") or endpoints.get("timeseries_scoring"))
    guidance = bool(endpoints.get("responses"))
    if scoring and guidance:
        return "Live models"
    if scoring:
        return "Live scoring"
    if guidance:
        return "Live guidance"
    return "Sample results"


def demo_page_html():
    """Read the checked-in, operator-facing home page without browser caching."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    return page.replace("__MODE_LABEL__", demo_mode_label()).replace("__SAMPLE_REQUESTS__", json.dumps(SAMPLE_REQUESTS).replace("<", "\\u003c"))

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=str(ROOT),**k)
    def log_message(self,*a): return
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
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
        if path not in PUBLIC_STATIC_PATHS:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_GET()
    def do_POST(self):
        try:
            p=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            route=urlparse(self.path).path
            result={"/api/predict":predict_request,"/api/forecast":forecast_volume,"/api/ask":retrieve_guidance,"/api/brief":operator_brief}.get(route)
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
    else:
        print("Live AutoRAG guidance enabled")
    ThreadingHTTPServer(("127.0.0.1",8000),Handler).serve_forever()
