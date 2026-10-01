#!/usr/bin/env python3
"""Upload bundled fixtures and submit the three City Services Copilot pipeline runs.

Copy .env.example to .env, fill in the connection and secret names, then run:
    python3 scripts/run_cluster_demo.py

The script follows the managed-pipeline/KFP flow used by AutoX CI. It never
downloads external data and never creates or changes Kubernetes secrets.
"""
from __future__ import annotations

import argparse
import signal
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def hard_timeout(seconds: int):
    """Interrupt a blocking network call on POSIX hosts, including DNS/connect stalls."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def expired(_signum, _frame):
        raise TimeoutError

    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def load_env(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"Environment file not found: {path}. Copy .env.example to .env first.")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f"Invalid .env line: {raw}")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and (not value or value.startswith("replace-with-") or value.startswith("YOUR-")):
        raise RuntimeError(f"Set {name} in .env")
    return value


def optional_env(name: str, default: str = "") -> str:
    """Read an optional setting, treating template placeholders as unset."""
    value = os.environ.get(name, default).strip()
    return "" if value.startswith("replace-with-") or "YOUR-" in value else value


def env_list(name: str) -> list[str]:
    value = env(name)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item for item in parsed):
        raise RuntimeError(f"{name} must be a JSON array or comma-separated model IDs")
    return parsed


def bool_env(name: str, default: bool = True) -> bool:
    return env(name, required=False, default=str(default)).lower() not in {"0", "false", "no", "off"}


def positive_int_env(name: str, default: int) -> int:
    value = env(name, required=False, default=str(default))
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def config() -> dict[str, object]:
    prefix = env("DEMO_S3_PREFIX", required=False, default="city-services-copilot").strip("/")
    bucket = env("AWS_S3_BUCKET")
    kfp_url = optional_env("RHOAI_KFP_URL").rstrip("/")
    rhoai_url = optional_env("RHOAI_URL").rstrip("/")
    if not kfp_url and not rhoai_url:
        raise RuntimeError("Set RHOAI_KFP_URL, or set RHOAI_URL to discover the existing pipeline Route")
    app_config_name = optional_env("DEMO_APP_CONFIG_PATH", "app-config.json")
    app_config_path = (ROOT / app_config_name).resolve()
    if ROOT.resolve() not in app_config_path.parents:
        raise RuntimeError("DEMO_APP_CONFIG_PATH must be inside this repository")
    return {
        "kfp_url": kfp_url + "/" if kfp_url else "",
        "rhoai_url": rhoai_url,
        "token": env("RHOAI_TOKEN"),
        "project": env("RHOAI_PROJECT_NAME"),
        "experiment": env("KFP_EXPERIMENT_NAME", required=False, default="city-services-copilot"),
        "verify_ssl": bool_env("KFP_VERIFY_SSL"),
        "openshift_verify_ssl": bool_env("RHOAI_OPENSHIFT_API_VERIFY_SSL"),
        "openshift_timeout": positive_int_env("RHOAI_OPENSHIFT_API_TIMEOUT_SECONDS", 20),
        "route_prefix": env("RHOAI_DSPA_ROUTE_PREFIX", required=False, default="ds-pipeline"),
        "s3_endpoint": env("AWS_S3_ENDPOINT"),
        "s3_access_key": env("AWS_ACCESS_KEY_ID"),
        "s3_secret_key": env("AWS_SECRET_ACCESS_KEY"),
        "s3_region": env("AWS_DEFAULT_REGION", required=False, default="us-east-1"),
        "s3_verify": bool_env("S3_SSL_VERIFY"),
        "bucket": bucket,
        "artifact_bucket": optional_env("PIPELINE_ARTIFACTS_S3_BUCKET") or bucket,
        "artifact_prefix": optional_env("PIPELINE_ARTIFACTS_S3_PREFIX").strip("/"),
        "prefix": prefix,
        "automl_secret": env("AUTOML_S3_SECRET_NAME"),
        "autorag_test_secret": env("AUTORAG_TEST_S3_SECRET_NAME"),
        "autorag_documents_secret": env("AUTORAG_DOCUMENTS_S3_SECRET_NAME"),
        "maas_secret": env("MAAS_SECRET_NAME"),
        "vector_db_secret": env("VECTOR_DB_SECRET_NAME"),
        "kserve_storage_key": env("AUTOML_KSERVE_STORAGE_KEY", required=False, default=env("AUTOML_S3_SECRET_NAME")),
        "serving_runtime": optional_env("AUTOML_SERVING_RUNTIME_NAME"),
        "create_serving_runtime": bool_env("AUTOML_CREATE_SERVING_RUNTIME", True),
        "serving_runtime_template": env("AUTOML_SERVING_RUNTIME_TEMPLATE_NAME", required=False, default="autogluon-runtime-template"),
        "serving_runtime_template_namespace": env("AUTOML_SERVING_RUNTIME_TEMPLATE_NAMESPACE", required=False, default="redhat-ods-applications"),
        "kserve_service_account": env("AUTOML_KSERVE_SERVICE_ACCOUNT", required=False),
        "deploy_cpu": env("AUTOML_DEPLOY_CPU", required=False, default="2"),
        "deploy_memory": env("AUTOML_DEPLOY_MEMORY", required=False, default="4Gi"),
        "deploy_timeout": positive_int_env("AUTOML_DEPLOY_TIMEOUT_SECONDS", 600),
        "app_config_path": app_config_path,
        "embedding_models": env_list("AUTORAG_EMBEDDING_MODELS"),
        "generation_models": env_list("AUTORAG_GENERATION_MODELS"),
        "tabular_pipeline": env("RHOAI_MANAGED_PIPELINE_TABULAR", required=False, default="autogluon-tabular-training-pipeline"),
        "timeseries_pipeline": env("RHOAI_MANAGED_PIPELINE_TIMESERIES", required=False, default="autogluon-timeseries-training-pipeline"),
        "autorag_pipeline": env("RHOAI_MANAGED_PIPELINE_AUTORAG", required=False, default="documents-rag-optimization-pipeline"),
        "automl_preset": env("AUTOML_PRESET", required=False, default="speed"),
        "autorag_preset": env("AUTORAG_PRESET", required=False, default="speed"),
        "autorag_metric": env("AUTORAG_OPTIMIZATION_METRIC", required=False, default="unitxt:faithfulness"),
        "autorag_patterns": int(env("AUTORAG_MAX_RAG_PATTERNS", required=False, default="5")),
    }


def validate_inputs(prefix: str) -> None:
    import subprocess
    subprocess.run([
        sys.executable, "scripts/validate_demo_inputs.py",
        "--document-prefix", f"{prefix}/autorag/guidance",
    ], cwd=ROOT, check=True)


def upload_data(settings: dict[str, object], dry_run: bool) -> None:
    prefix = settings["prefix"]
    files = [
        (ROOT / "data/prepared/resolution-risk.csv", f"{prefix}/automl/resolution-risk.csv"),
        (ROOT / "data/prepared/service-demand-daily.csv", f"{prefix}/automl/service-demand-daily.csv"),
        (ROOT / "data/autorag-evaluation.json", f"{prefix}/autorag/autorag-evaluation.json"),
    ]
    files.extend((path, f"{prefix}/autorag/guidance/{path.name}") for path in sorted((ROOT / "data/guidance").glob("*.md")))
    if dry_run:
        for path, key in files:
            print(f"Would upload {path.relative_to(ROOT)} -> s3://{settings['bucket']}/{key}")
        return
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError("Install requirements-cluster.txt before a non-dry run") from error
    client = boto3.client(
        "s3", endpoint_url=settings["s3_endpoint"],
        aws_access_key_id=settings["s3_access_key"], aws_secret_access_key=settings["s3_secret_key"],
        region_name=settings["s3_region"], verify=settings["s3_verify"],
    )
    for path, key in files:
        print(f"Uploading {path.relative_to(ROOT)} -> s3://{settings['bucket']}/{key}")
        client.upload_file(str(path), settings["bucket"], key)


def make_client(settings: dict[str, object]):
    try:
        import kfp
    except ImportError as error:
        raise RuntimeError("Install requirements-cluster.txt before a non-dry run") from error
    kfp_url = settings["kfp_url"] or discover_kfp_url(settings)
    settings["resolved_kfp_url"] = kfp_url
    return kfp.Client(
        host=kfp_url, namespace=settings["project"],
        existing_token=settings["token"], verify_ssl=settings["verify_ssl"],
    )


def discover_kfp_url(settings: dict[str, object]) -> str:
    """Discover an existing Data Science Pipelines Route from the OpenShift API."""
    try:
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException
    except ImportError as error:
        raise RuntimeError("Install requirements-cluster.txt to discover RHOAI_KFP_URL from RHOAI_URL") from error
    kube_config = client.Configuration()
    kube_config.host = settings["rhoai_url"]
    kube_config.verify_ssl = settings["openshift_verify_ssl"]
    kube_config.api_key["authorization"] = settings["token"]
    kube_config.api_key_prefix["authorization"] = "Bearer"
    timeout = settings["openshift_timeout"]
    print(f"Discovering the {settings['route_prefix']!r} pipeline Route through {settings['rhoai_url']} (timeout: {timeout}s)...", flush=True)
    try:
        with hard_timeout(timeout):
            routes = client.CustomObjectsApi(client.ApiClient(kube_config)).list_namespaced_custom_object(
                group="route.openshift.io", version="v1", namespace=settings["project"], plural="routes",
                _request_timeout=(timeout, timeout),
            ).get("items", [])
    except ApiException as error:
        if error.status in {401, 403}:
            raise RuntimeError(
                f"OpenShift API rejected the token while reading Routes in project {settings['project']!r}. "
                "Refresh RHOAI_TOKEN and confirm it can list Routes in that project."
            ) from error
        raise RuntimeError(
            f"OpenShift API returned HTTP {error.status} while discovering the pipeline Route. "
            "Confirm RHOAI_URL, RHOAI_PROJECT_NAME, and cluster access."
        ) from error
    except TimeoutError as error:
        raise RuntimeError(
            f"Timed out after {timeout}s while connecting to the OpenShift API at {settings['rhoai_url']}. "
            "Run `oc whoami --show-server` and copy that exact URL into RHOAI_URL; "
            "then check VPN/network access or set RHOAI_KFP_URL directly."
        ) from error
    except Exception as error:
        raise RuntimeError(
            f"Could not reach the OpenShift API at {settings['rhoai_url']} within {timeout}s while discovering the pipeline Route. "
            "Run `oc whoami --show-server` to verify RHOAI_URL, then check VPN/network access; "
            "or set RHOAI_KFP_URL directly."
        ) from error
    route = next((item for item in routes if (item.get("metadata") or {}).get("name", "").startswith(settings["route_prefix"])), None)
    if route is None:
        names = [item.get("metadata", {}).get("name", "") for item in routes]
        raise RuntimeError(f"No {settings['route_prefix']!r} Route found in {settings['project']!r}. Available Routes: {names}")
    spec = route.get("spec") or {}
    status = route.get("status") or {}
    host = spec.get("host") or next((entry.get("host") for entry in status.get("ingress", []) if entry.get("host")), "")
    if not host:
        raise RuntimeError(f"Pipeline Route {(route.get('metadata') or {}).get('name')!r} has no host")
    resolved = f"https://{host}/"
    print(f"Discovered KFP route: {resolved}")
    return resolved


def run_state(detail) -> str:
    run = getattr(detail, "run", detail)
    for name in ("state", "status", "phase"):
        value = getattr(run, name, None)
        if value is not None:
            text = getattr(value, "name", str(value))
            return text.rsplit(".", 1)[-1].upper()
    return "UNKNOWN"


def is_terminal(state: str) -> bool:
    return state in {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR", "SKIPPED"}


def is_success(state: str) -> bool:
    return state in {"SUCCEEDED", "COMPLETED"}


def use_color() -> bool:
    """Use color only for an interactive terminal, respecting the NO_COLOR convention."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color() else text


def status_label(state: str) -> str:
    if is_success(state):
        return paint(f"✓ {state}", "1;32")
    if is_terminal(state):
        return paint(f"✗ {state}", "1;31")
    return paint(f"• {state}", "1;33")


def score_text(score: object) -> str:
    if isinstance(score, (int, float)):
        return f"{score:.6f}".rstrip("0").rstrip(".")
    return str(score)


def print_table(headers: tuple[str, ...], rows: list[tuple[object, ...]], *, best_row: bool = False) -> None:
    """Print a small dependency-free terminal table with an optional highlighted winner."""
    rendered = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("    " + paint("  ".join(header.ljust(width) for header, width in zip(headers, widths)), "1;36"))
    print("    " + paint("  ".join("─" * width for width in widths), "2"))
    for index, row in enumerate(rendered):
        cells = [cell.ljust(width) for cell, width in zip(row, widths)]
        line = "  ".join(cells)
        print("    " + (paint(line, "1;32") if best_row and index == 0 else line))


def run_url(settings: dict[str, object], run_id: str) -> str:
    return f"{settings['resolved_kfp_url'].rstrip('/')}/#/runs/details/{run_id}"


def artifact_client(settings: dict[str, object]):
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError("Install requirements-cluster.txt to retrieve leaderboards") from error
    return boto3.client(
        "s3", endpoint_url=settings["s3_endpoint"],
        aws_access_key_id=settings["s3_access_key"], aws_secret_access_key=settings["s3_secret_key"],
        region_name=settings["s3_region"], verify=settings["s3_verify"],
    )


def list_s3_keys(client, bucket: str, prefix: str) -> list[str]:
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return keys


def artifact_prefix(settings: dict[str, object], suffix: str) -> str:
    root = settings["artifact_prefix"]
    return f"{root}/{suffix}" if root else suffix


def report_leaderboard(settings: dict[str, object], run_id: str) -> None:
    """Print completed AutoML or AutoRAG leaderboard data from pipeline artifacts."""
    try:
        client = artifact_client(settings)
        bucket = settings["artifact_bucket"]
        metric_keys = []
        html_keys = []
        for pipeline_name, folder in (
            (settings["tabular_pipeline"], "autogluon-models-training"),
            (settings["tabular_pipeline"], "autogluon-models-training-2"),
            (settings["tabular_pipeline"], "leaderboard-evaluation"),
            (settings["timeseries_pipeline"], "autogluon-timeseries-models-training"),
            (settings["timeseries_pipeline"], "autogluon-timeseries-models-training-2"),
            (settings["timeseries_pipeline"], "timeseries-leaderboard-evaluation"),
        ):
            keys = list_s3_keys(client, bucket, artifact_prefix(settings, f"{pipeline_name}/{run_id}/{folder}/"))
            metric_keys.extend(key for key in keys if "/models_artifact/" in key and key.endswith("/metrics/metrics.json"))
            html_keys.extend(key for key in keys if "html_artifact" in key)
        if metric_keys:
            print("  " + paint("AutoML leaderboard", "1"))
            rows = []
            for key in sorted(set(metric_keys)):
                model = key.split("/models_artifact/", 1)[1].split("/", 1)[0]
                metrics = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
                metric_name = next((name for name in ("score_val", "score_test", "accuracy", "roc_auc", "mean_absolute_scaled_error", "weighted_quantile_loss") if metrics.get(name) is not None), "n/a")
                rows.append((model, metric_name, metrics.get(metric_name, "n/a")))
            # AutoGluon persists loss-style evaluation values as negated scores, so every
            # value here follows its common higher-is-better leaderboard convention.
            ranked = sorted(rows, key=lambda row: row[2] if isinstance(row[2], (int, float)) else float("-inf"), reverse=True)
            print_table(
                ("Rank", "Model", "Metric", "Score"),
                [(index, model, metric_name, score_text(score)) for index, (model, metric_name, score) in enumerate(ranked, start=1)],
                best_row=True,
            )
            for key in sorted(set(html_keys)):
                print(f"    {paint('HTML leaderboard:', '2')} s3://{bucket}/{key}")
            return

        rag_prefix = artifact_prefix(settings, f"documents-rag-optimization-pipeline/{run_id}/")
        pattern_keys = [key for key in list_s3_keys(client, bucket, rag_prefix) if key.endswith("/pattern.json")]
        if pattern_keys:
            print("  " + paint("AutoRAG leaderboard", "1"))
            rows = []
            for key in pattern_keys:
                payload = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
                score = payload.get("final_score")
                metric_name = payload.get("optimization_metric") or settings["autorag_metric"]
                for metric in (payload.get("evaluation") or {}).get("metrics", []):
                    if metric.get("optimization_metric"):
                        if score is None:
                            score = (metric.get("scores") or {}).get("mean")
                        metric_name = (
                            metric.get("name")
                            or metric.get("metric_name")
                            or metric.get("id")
                            or metric.get("metric")
                            or metric_name
                        )
                        break
                rows.append((key.split("/rag_patterns/", 1)[-1].split("/", 1)[0], metric_name, score))
            ranked = sorted(rows, key=lambda row: row[2] if isinstance(row[2], (int, float)) else float("-inf"), reverse=True)
            print_table(
                ("Rank", "Pattern", "Metric", "Score"),
                [(index, pattern, metric_name, score_text(score) if score is not None else "n/a") for index, (pattern, metric_name, score) in enumerate(ranked, start=1)],
                best_row=True,
            )
            print(f"    {paint('Pattern artifacts:', '2')} s3://{bucket}/{rag_prefix}")
            return
        print("  " + paint("Leaderboard artifacts are not available yet.", "2"))
    except Exception as error:
        print(f"  {paint('Could not retrieve leaderboard artifacts:', '1;31')} {error}")


def select_best_automl_model(settings: dict[str, object], run_id: str) -> dict[str, object]:
    """Return the best scored AutoML predictor artifact for a completed run."""
    client = artifact_client(settings)
    bucket = settings["artifact_bucket"]
    candidates = []
    for pipeline_name, folders in {
        settings["tabular_pipeline"]: ("autogluon-models-training", "autogluon-models-training-2"),
        settings["timeseries_pipeline"]: ("autogluon-timeseries-models-training", "autogluon-timeseries-models-training-2"),
    }.items():
        for folder in folders:
            prefix = artifact_prefix(settings, f"{pipeline_name}/{run_id}/{folder}/")
            for key in list_s3_keys(client, bucket, prefix):
                if not key.endswith("/metrics/metrics.json") or "/models_artifact/" not in key:
                    continue
                metrics = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
                metric_name = next((name for name in ("score_val", "score_test", "accuracy", "roc_auc", "mean_absolute_scaled_error", "weighted_quantile_loss") if metrics.get(name) is not None), None)
                if metric_name is None or not isinstance(metrics[metric_name], (int, float)):
                    continue
                model = key.split("/models_artifact/", 1)[1].split("/", 1)[0]
                predictor_prefix = key.rsplit("/metrics/metrics.json", 1)[0] + "/predictor/"
                candidates.append({
                    "pipeline": pipeline_name, "model": model, "metric": metric_name,
                    "score": metrics[metric_name], "predictor_prefix": predictor_prefix,
                })
    if not candidates:
        raise RuntimeError(f"No AutoML metrics artifacts found for run {run_id!r}")
    # Pipeline score artifacts use AutoGluon's higher-is-better convention, including negated losses.
    chosen = max(candidates, key=lambda item: item["score"])
    predictor_keys = list_s3_keys(client, bucket, chosen["predictor_prefix"])
    if not any(key.endswith("/predictor.pkl") for key in predictor_keys):
        raise RuntimeError(f"Selected model {chosen['model']!r} has no predictor.pkl artifact")
    return chosen


def kserve_client(settings: dict[str, object]):
    if not settings["rhoai_url"]:
        raise RuntimeError("KServe deployment requires RHOAI_URL in .env")
    try:
        from kubernetes import client
    except ImportError as error:
        raise RuntimeError("Install requirements-cluster.txt before deploying a model") from error
    kube_config = client.Configuration()
    kube_config.host = settings["rhoai_url"]
    kube_config.verify_ssl = settings["openshift_verify_ssl"]
    kube_config.api_key["authorization"] = settings["token"]
    kube_config.api_key_prefix["authorization"] = "Bearer"
    return client.CustomObjectsApi(client.ApiClient(kube_config))


def serving_runtime_exists(client, settings: dict[str, object], name: str) -> bool:
    from kubernetes.client.rest import ApiException

    try:
        client.get_namespaced_custom_object(
            group="serving.kserve.io", version="v1alpha1", namespace=settings["project"],
            plural="servingruntimes", name=name, _request_timeout=30,
        )
        return True
    except ApiException as error:
        if error.status == 404:
            return False
        raise RuntimeError(f"Could not check ServingRuntime {name!r}: HTTP {error.status}") from error


def create_serving_runtime_from_template(client, settings: dict[str, object], runtime_name: str) -> bool:
    """Clone the OpenShift AI AutoGluon runtime template into the demo project."""
    if serving_runtime_exists(client, settings, runtime_name):
        print(f"Reusing existing ServingRuntime {runtime_name}.")
        return False
    template_name = settings["serving_runtime_template"]
    template_namespace = settings["serving_runtime_template_namespace"]
    try:
        template = client.get_namespaced_custom_object(
            group="template.openshift.io", version="v1", namespace=template_namespace,
            plural="templates", name=template_name, _request_timeout=30,
        )
    except Exception as error:
        raise RuntimeError(
            f"Could not read OpenShift Template {template_name!r} in {template_namespace!r}. "
            "Ask a platform administrator for access, or set AUTOML_SERVING_RUNTIME_NAME "
            "to an existing namespace-scoped AutoGluon ServingRuntime and set "
            "AUTOML_CREATE_SERVING_RUNTIME=false."
        ) from error
    embedded = next((item for item in template.get("objects", []) if item.get("kind") == "ServingRuntime" and item.get("apiVersion") == "serving.kserve.io/v1alpha1"), None)
    if embedded is None:
        raise RuntimeError(f"OpenShift Template {template_name!r} contains no ServingRuntime object")
    allowed_annotations = {
        "opendatahub.io/apiProtocol", "opendatahub.io/runtime-version", "openshift.io/display-name",
        "monitoring.opendatahub.io/scrape", "opendatahub.io/kserve-runtime",
        "prometheus.io/path", "prometheus.io/port",
    }
    runtime = {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ServingRuntime",
        "metadata": {
            "name": runtime_name,
            "namespace": settings["project"],
            "annotations": {key: value for key, value in (embedded.get("metadata", {}).get("annotations", {}) or {}).items() if key in allowed_annotations},
            "labels": {"opendatahub.io/dashboard": "true"},
        },
        "spec": embedded.get("spec"),
    }
    client.create_namespaced_custom_object(
        group="serving.kserve.io", version="v1alpha1", namespace=settings["project"],
        plural="servingruntimes", body=runtime, _request_timeout=30,
    )
    print(f"Created ServingRuntime {runtime_name} from template {template_name}.")
    return True


def runtime_for_deployment(client, settings: dict[str, object], endpoint_name: str, dry_run: bool) -> str:
    """Resolve an explicit runtime or provision the AutoGluon template runtime for one endpoint."""
    explicit_runtime = settings["serving_runtime"]
    if explicit_runtime:
        if not dry_run and not serving_runtime_exists(client, settings, explicit_runtime):
            raise RuntimeError(
                f"Configured AUTOML_SERVING_RUNTIME_NAME {explicit_runtime!r} does not exist in {settings['project']!r}."
            )
        return explicit_runtime
    if not settings["create_serving_runtime"]:
        raise RuntimeError(
            "Set AUTOML_SERVING_RUNTIME_NAME to an existing AutoGluon ServingRuntime, or set "
            "AUTOML_CREATE_SERVING_RUNTIME=true to create one from the configured template."
        )
    if dry_run:
        return endpoint_name
    created = create_serving_runtime_from_template(client, settings, endpoint_name)
    if created:
        print("Waiting 30s for KServe to index the new ServingRuntime...", flush=True)
        endpoint_spinner_wait(30, endpoint_name, "initializing runtime")
    return endpoint_name


def inference_service_manifest(settings: dict[str, object], name: str, predictor_prefix: str, runtime_name: str) -> dict:
    storage_key = settings["kserve_storage_key"]
    service_account = settings["kserve_service_account"] or f"{storage_key}-sa"
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": name,
            "namespace": settings["project"],
            "labels": {"networking.kserve.io/visibility": "exposed", "opendatahub.io/dashboard": "true"},
            "annotations": {
                "serving.kserve.io/stop": "false",
                "serving.kserve.io/deploymentMode": "RawDeployment",
                "security.opendatahub.io/enable-auth": "true",
                "opendatahub.io/connections": storage_key,
                "opendatahub.io/connection-path": predictor_prefix.rstrip("/"),
                "opendatahub.io/model-type": "predictive",
            },
        },
        "spec": {"predictor": {
            "automountServiceAccountToken": False,
            "serviceAccountName": service_account,
            "deploymentStrategy": {"type": "RollingUpdate"},
            "minReplicas": 1, "maxReplicas": 1,
            "model": {
                "modelFormat": {"name": "autogluon", "version": "1"},
                "runtime": runtime_name,
                "resources": {"requests": {"cpu": settings["deploy_cpu"], "memory": settings["deploy_memory"]}, "limits": {"cpu": settings["deploy_cpu"], "memory": settings["deploy_memory"]}},
                "storage": {"key": storage_key, "path": predictor_prefix.rstrip("/")},
            },
        }},
    }


def wait_for_inference_service(client, settings: dict[str, object], name: str) -> dict:
    """Wait for KServe readiness without flooding an interactive terminal."""
    deadline = time.monotonic() + settings["deploy_timeout"]
    live_wait = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    previous_status = None
    while time.monotonic() < deadline:
        service = client.get_namespaced_custom_object(group="serving.kserve.io", version="v1beta1", namespace=settings["project"], plural="inferenceservices", name=name, _request_timeout=30)
        conditions = (service.get("status") or {}).get("conditions") or []
        ready = next((condition for condition in conditions if condition.get("type") == "Ready"), None)
        if ready and ready.get("status") == "True":
            return service
        state = ready.get("status", "Unknown") if ready else "Pending"
        reason = ready.get("reason", "") if ready else ""
        status = f"{state}{f': {reason}' if reason else ''}"
        if not live_wait and status != previous_status:
            print(f"  {name}: readiness {status}")
        previous_status = status
        remaining = max(0, deadline - time.monotonic())
        if live_wait:
            endpoint_spinner_wait(min(15, remaining), name, status)
        else:
            time.sleep(min(15, remaining))
    raise RuntimeError(f"InferenceService {name!r} did not become ready within {settings['deploy_timeout']}s")


def endpoint_spinner_wait(seconds: float, name: str, status: str) -> None:
    """Render one animated readiness line while KServe reconciles an endpoint."""
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    deadline = time.monotonic() + seconds
    frame = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        message = f"{frames[frame % len(frames)]} Deploying {name} — readiness {status}; checking again in {max(1, round(remaining))}s"
        sys.stdout.write(f"\r\033[K{paint(message, '33')}")
        sys.stdout.flush()
        time.sleep(min(0.15, remaining))
        frame += 1
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def inference_url(service: dict, name: str) -> str:
    status = service.get("status") or {}
    base_url = status.get("url") or (status.get("address") or {}).get("url")
    if not base_url:
        raise RuntimeError(f"InferenceService {name!r} is ready but did not report a public URL")
    return f"{base_url.rstrip('/')}/v1/models/{name}:predict"


def update_app_config(settings: dict[str, object], endpoints: dict[str, str]) -> None:
    """Merge ready native KServe endpoints into the app's private configuration."""
    path = settings["app_config_path"]
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Cannot update {path.name}: it is not valid JSON") from error
        if not isinstance(config, dict):
            raise RuntimeError(f"Cannot update {path.name}: it must contain a JSON object")
    else:
        config = {}
    current_endpoints = config.get("endpoints") or {}
    if not isinstance(current_endpoints, dict):
        raise RuntimeError(f"Cannot update {path.name}: endpoints must be a JSON object")
    current_endpoints.update(endpoints)
    config["endpoints"] = current_endpoints
    config["endpoint_protocol"] = "kserve-v1"
    endpoint_tokens = config.get("endpoint_tokens") or {}
    if not isinstance(endpoint_tokens, dict):
        raise RuntimeError(f"Cannot update {path.name}: endpoint_tokens must be a JSON object")
    endpoint_tokens.update({name: settings["token"] for name in endpoints})
    config["endpoint_tokens"] = endpoint_tokens
    if not isinstance(config.get("api_token"), str) or not config["api_token"].strip() or config["api_token"].startswith("replace-with-"):
        config["api_token"] = settings["token"]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    configured = ", ".join(sorted(endpoints))
    print(f"  Updated {path.relative_to(ROOT)} with {configured}; it remains Git-ignored.")


def deploy_automl_runs(settings: dict[str, object], run_ids: list[str], wait: bool, dry_run: bool) -> None:
    from kubernetes.client.rest import ApiException

    client = None if dry_run else kserve_client(settings)
    app_endpoints = {}
    for run_id in run_ids:
        selected = select_best_automl_model(settings, run_id)
        kind = "tabular" if selected["pipeline"] == settings["tabular_pipeline"] else "timeseries"
        name = f"city-services-{kind}-{run_id[:8]}"
        runtime_name = runtime_for_deployment(client, settings, name, dry_run)
        body = inference_service_manifest(settings, name, selected["predictor_prefix"], runtime_name)
        if dry_run:
            if not settings["serving_runtime"]:
                print(f"Would create ServingRuntime {runtime_name} from template {settings['serving_runtime_template']}.")
            print(f"Would create {name} from {selected['model']} ({selected['metric']}={selected['score']}) using runtime {runtime_name}:")
            print(json.dumps(body, indent=2))
            continue
        try:
            client.create_namespaced_custom_object(group="serving.kserve.io", version="v1beta1", namespace=settings["project"], plural="inferenceservices", body=body, _request_timeout=30)
            print(f"Created {name} from {selected['model']} ({selected['metric']}={selected['score']}).")
        except ApiException as error:
            if error.status != 409:
                raise RuntimeError(f"Could not create InferenceService {name!r}: HTTP {error.status}") from error
            print(f"Reusing existing InferenceService {name}.")
        if wait:
            service = wait_for_inference_service(client, settings, name)
            status = service.get("status") or {}
            print(f"  Ready endpoint: {status.get('url') or (status.get('address') or {}).get('url') or 'check the OpenShift AI dashboard'}")
            app_endpoints[f"{kind}_scoring"] = inference_url(service, name)
        else:
            service = client.get_namespaced_custom_object(group="serving.kserve.io", version="v1beta1", namespace=settings["project"], plural="inferenceservices", name=name, _request_timeout=30)
            ready = next((condition for condition in (service.get("status") or {}).get("conditions", []) if condition.get("type") == "Ready"), None)
            if ready and ready.get("status") == "True":
                app_endpoints[f"{kind}_scoring"] = inference_url(service, name)
            else:
                print(f"  {name} is not ready yet; app configuration was not updated for it.")
    if app_endpoints and not dry_run:
        update_app_config(settings, app_endpoints)


def spinner_wait(seconds: int, states: dict[str, str]) -> None:
    """Show an in-place spinner while waiting for the next KFP status poll."""
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    labels = {
        "SUCCEEDED": "completed", "COMPLETED": "completed", "FAILED": "failed",
        "ERROR": "failed", "CANCELLED": "cancelled", "SKIPPED": "skipped",
        "RUNNING": "running", "PENDING": "queued", "QUEUED": "queued",
    }
    counts = {}
    for state in states.values():
        label = labels.get(state, state.lower().replace("_", " "))
        counts[label] = counts.get(label, 0) + 1
    order = {"running": 0, "queued": 1, "completed": 2, "failed": 3, "cancelled": 4, "skipped": 5}
    summary = " · ".join(f"{count} {label}" for label, count in sorted(counts.items(), key=lambda item: order.get(item[0], 99)))
    deadline = time.monotonic() + seconds
    frame = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        message = f"{frames[frame % len(frames)]} {summary} — checking again in {max(1, round(remaining))}s"
        sys.stdout.write(f"\r\033[K{paint(message, '33')}")
        sys.stdout.flush()
        time.sleep(min(0.15, remaining))
        frame += 1
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def print_run_status(settings: dict[str, object], run_id: str, state: str) -> None:
    print(f"{status_label(state)}  {paint(run_id, '1')}")
    print(f"  {paint('Run details:', '2')} {run_url(settings, run_id)}")


def monitor_runs(client, settings: dict[str, object], run_ids: list[str], watch: bool, poll_interval: int) -> dict[str, str]:
    normalized_ids = [run_id.strip() for run_id in run_ids]
    if not all(normalized_ids):
        raise RuntimeError(
            "--status received an empty run ID. When spanning lines, place each backslash as the final character on its line."
        )
    reported = set()
    previous_states = {}
    live_watch = watch and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    announced = False
    while True:
        pending = False
        states = {}
        for run_id in normalized_ids:
            try:
                detail = client.get_run(run_id)
            except Exception as error:
                raise RuntimeError(f"Could not retrieve KFP run {run_id!r}. Verify the run ID and selected KFP route.") from error
            state = run_state(detail)
            states[run_id] = state
            if not is_terminal(state):
                pending = True
            if not live_watch and previous_states.get(run_id) != state:
                print_run_status(settings, run_id, state)
            elif live_watch and is_terminal(state) and run_id not in reported:
                print_run_status(settings, run_id, state)
            if is_terminal(state) and run_id not in reported:
                reported.add(run_id)
                if is_success(state):
                    report_leaderboard(settings, run_id)
                print()
        if live_watch and not announced:
            print(f"Watching {len(normalized_ids)} run(s). Press Ctrl-C to stop watching; the runs continue on the cluster.")
            for run_id in normalized_ids:
                print(f"  {run_id}: {run_url(settings, run_id)}")
            print()
            announced = True
        if not watch or not pending:
            return states
        previous_states = states
        if live_watch:
            spinner_wait(poll_interval, states)
        else:
            print(f"Waiting {poll_interval}s before the next status check...", flush=True)
            time.sleep(poll_interval)


def pipeline_target(client, display_name: str) -> tuple[str, str | None]:
    pipeline_id = client.get_pipeline_id(display_name)
    if not pipeline_id:
        raise RuntimeError(f"Managed pipeline not found: {display_name}")
    versions = client.list_pipeline_versions(pipeline_id=pipeline_id, page_size=1, sort_by="created_at desc")
    latest = (getattr(versions, "pipeline_versions", None) or [None])[0]
    return pipeline_id, getattr(latest, "pipeline_version_id", None)


def experiment_id(client, name: str, project: str) -> str:
    try:
        return client.create_experiment(name=name, namespace=project).experiment_id
    except Exception as error:
        if "already exists" not in str(error).lower() and "conflict" not in str(error).lower():
            raise
        return client.get_experiment(experiment_name=name, namespace=project).experiment_id


def parameters(settings: dict[str, object]) -> dict[str, dict[str, object]]:
    bucket, prefix = settings["bucket"], settings["prefix"]
    return {
        "tabular": {
            "train_data_secret_name": settings["automl_secret"], "train_data_bucket_name": bucket,
            "train_data_file_key": f"{prefix}/automl/resolution-risk.csv", "label_column": "sla_miss",
            "task_type": "binary", "positive_class": "1", "top_n": 3, "preset": settings["automl_preset"],
        },
        "timeseries": {
            "train_data_secret_name": settings["automl_secret"], "train_data_bucket_name": bucket,
            "train_data_file_key": f"{prefix}/automl/service-demand-daily.csv", "target": "target",
            "timestamp_column": "timestamp", "id_column": "item_id", "prediction_length": 7,
            "top_n": 3, "preset": settings["automl_preset"],
        },
        "autorag": {
            "test_data_secret_name": settings["autorag_test_secret"], "test_data_bucket_name": bucket,
            "test_data_key": f"{prefix}/autorag/autorag-evaluation.json",
            "input_data_secret_name": settings["autorag_documents_secret"], "input_data_bucket_name": bucket,
            "input_data_keys": [f"{prefix}/autorag/guidance/"], "maas_secret_name": settings["maas_secret"],
            "db_secret_name": settings["vector_db_secret"], "embedding_models": settings["embedding_models"],
            "generation_models": settings["generation_models"], "optimization_metric": settings["autorag_metric"],
            "optimization_max_rag_patterns": settings["autorag_patterns"], "preset": settings["autorag_preset"],
        },
    }


def print_submission_plan(pipelines: dict[str, object], run_parameters: dict[str, dict[str, object]], selected: list[str], show_parameters: bool) -> None:
    """Render the dry-run plan in terms a demo user can verify at a glance."""
    titles = {"tabular": "Tabular AutoML — SLA-miss risk", "timeseries": "Time-series AutoML — request demand", "autorag": "AutoRAG — grounded service guidance"}
    print("\n" + paint("PREFLIGHT PREVIEW — no changes have been made yet.", "1;33"))
    print(paint("Submission plan", "1"))
    for index, name in enumerate(selected, start=1):
        params = run_parameters[name]
        print(f"\n  {index}. {paint(titles[name], '1;36')}")
        print(f"     Pipeline:   {pipelines[name]}")
        if name == "tabular":
            print(f"     Training:   s3://{params['train_data_bucket_name']}/{params['train_data_file_key']}")
            print(f"     Connection: {params['train_data_secret_name']}")
            print(f"     Target:     {params['label_column']} ({params['task_type']}, positive class {params['positive_class']})")
            print(f"     Training:   {params['preset']} preset; retain top {params['top_n']} models")
        elif name == "timeseries":
            print(f"     Training:   s3://{params['train_data_bucket_name']}/{params['train_data_file_key']}")
            print(f"     Connection: {params['train_data_secret_name']}")
            print(f"     Forecast:   {params['prediction_length']} days; target={params['target']}; series={params['id_column']}; time={params['timestamp_column']}")
            print(f"     Training:   {params['preset']} preset; retain top {params['top_n']} models")
        else:
            print(f"     Evaluation: s3://{params['test_data_bucket_name']}/{params['test_data_key']}")
            print(f"     Corpus:     s3://{params['input_data_bucket_name']}/{params['input_data_keys'][0]}")
            print(f"     Connections: evaluation={params['test_data_secret_name']}; documents={params['input_data_secret_name']}; MaaS={params['maas_secret_name']}; vector DB={params['db_secret_name']}")
            print(f"     Models:     embedding={', '.join(params['embedding_models'])}; generation={', '.join(params['generation_models'])}")
            print(f"     Optimize:   {params['optimization_metric']}; evaluate up to {params['optimization_max_rag_patterns']} patterns ({params['preset']} preset)")
        if show_parameters:
            print(paint("     Full parameters:", "2"))
            for line in json.dumps(params, indent=2).splitlines():
                print(f"       {line}")


def submit(client, settings: dict[str, object], selected: list[str], dry_run: bool, show_parameters: bool = False) -> dict[str, str]:
    run_parameters = parameters(settings)
    pipelines = {"tabular": settings["tabular_pipeline"], "timeseries": settings["timeseries_pipeline"], "autorag": settings["autorag_pipeline"]}
    if dry_run:
        print_submission_plan(pipelines, run_parameters, selected, show_parameters)
        return {}
    experiment = experiment_id(client, settings["experiment"], settings["project"])
    submitted = {}
    for name in selected:
        pipeline_id, version_id = pipeline_target(client, pipelines[name])
        run = client.run_pipeline(
            experiment_id=experiment, job_name=f"city-services-copilot-{name}-{int(time.time())}",
            pipeline_id=pipeline_id, version_id=version_id, params=run_parameters[name], enable_caching=False,
        )
        print(f"Submitted {name}: run ID {run.run_id}")
        submitted[name] = run.run_id
    return submitted


def confirm(question: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"\n{question} [{hint}]: ").strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in {"y", "yes"}


def post_submit_guide(client, settings: dict[str, object], submitted: dict[str, str], args) -> None:
    """Offer the normal watch and deployment handoff after a successful submission."""
    run_ids = list(submitted.values())
    status_command = "python3 scripts/run_cluster_demo.py --status " + " ".join(run_ids) + " --watch"
    interactive = sys.stdin.isatty() and not args.non_interactive
    if not interactive:
        print(f"\nRuns submitted. To monitor them later:\n  {status_command}")
        return

    if args.wait or confirm("Watch the submitted runs until they finish", default=True):
        states = monitor_runs(client, settings, run_ids, watch=True, poll_interval=args.poll_interval)
    else:
        print(f"\nRuns are still executing. Monitor them later with:\n  {status_command}")
        return

    automl_run_ids = [
        submitted[name]
        for name in ("tabular", "timeseries")
        if name in submitted and is_success(states.get(submitted[name], "UNKNOWN"))
    ]
    if not automl_run_ids:
        print("No successful AutoML runs are available to deploy.")
        return

    deploy_command = "python3 scripts/run_cluster_demo.py --deploy " + " ".join(automl_run_ids) + " --wait-deploy"
    print("\nThe best model from each successful AutoML run is ready for optional deployment.")
    for name in ("tabular", "timeseries"):
        run_id = submitted.get(name)
        if run_id in automl_run_ids:
            print(f"  {name:<10} {run_id}")
    if not confirm("Would you like to automatically deploy these trained models as KServe scoring endpoints", default=False):
        print(f"Deploy them later with:\n  {deploy_command}")
        return
    wait_for_endpoints = confirm("Wait for the scoring endpoint(s) to become ready", default=True)
    deploy_automl_runs(settings, automl_run_ids, wait_for_endpoints, dry_run=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--skip-upload", action="store_true", help="submit runs without uploading the bundled files")
    parser.add_argument("--only", choices=("all", "tabular", "timeseries", "autorag"), default="all")
    parser.add_argument("--wait", action="store_true", help="watch all submitted runs immediately, without asking first")
    parser.add_argument("--status", metavar="RUN_ID", nargs="+", help="show status and completed-run leaderboard(s) for existing KFP run IDs")
    parser.add_argument("--watch", action="store_true", help="with --status, poll until all runs reach a terminal state")
    parser.add_argument("--deploy", metavar="RUN_ID", nargs="+", help="deploy the best model from completed AutoML run ID(s) as KServe InferenceServices")
    parser.add_argument("--wait-deploy", action="store_true", help="with --deploy, wait for each InferenceService to become ready")
    parser.add_argument("--poll-interval", type=int, default=15, help="status polling interval in seconds (default: 15)")
    parser.add_argument("--non-interactive", action="store_true", help="do not prompt to watch or deploy after submitting runs")
    parser.add_argument("--dry-run", action="store_true", help="validate configuration and print actions without network writes")
    parser.add_argument("--show-parameters", action="store_true", help="with --dry-run, include the full KFP parameter JSON after each summary")
    args = parser.parse_args()
    try:
        if args.poll_interval <= 0:
            raise RuntimeError("--poll-interval must be a positive integer")
        load_env(args.env_file)
        settings = config()
        if args.deploy:
            run_ids = [run_id.strip() for run_id in args.deploy]
            if not all(run_ids):
                raise RuntimeError(
                    "--deploy received an empty run ID. When spanning lines, place each backslash as the final character on its line."
                )
            deploy_automl_runs(settings, run_ids, args.wait_deploy, args.dry_run)
            return
        if args.status:
            monitor_runs(make_client(settings), settings, args.status, args.watch, args.poll_interval)
            return
        validate_inputs(settings["prefix"])
        selected = [args.only] if args.only != "all" else ["tabular", "timeseries", "autorag"]
        if not args.skip_upload:
            upload_data(settings, args.dry_run)
        if args.dry_run:
            submit(None, settings, selected, True, args.show_parameters)
        else:
            client = make_client(settings)
            submitted = submit(client, settings, selected, False)
            post_submit_guide(client, settings, submitted, args)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Any pipeline runs already submitted continue on the cluster.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
