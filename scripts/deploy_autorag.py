"""Deploy the generated AutoRAG OpenShell starter kit and connect the local app.

Pinned downstream templates own the build, registry push, sandbox, and Route flow.
Credentials stay outside the build context and are never printed.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


def cluster_tool_path() -> str:
    """Include user-installed deployment tools without shell profile edits."""
    candidates = (Path.home() / ".local/bin",)
    paths = [str(directory) for directory in candidates if directory.is_dir()]
    paths.extend(os.environ.get("PATH", "").split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(paths))


def find_artifacts(client, bucket: str, prefix: str, kit_key: str = "") -> str:
    """Find the run-level starter kit independently of individual pattern artifacts."""
    candidates = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        candidates.extend(item["Key"] for item in page.get("Contents", [])
                          if item["Key"].startswith(prefix) and item["Key"].endswith("/starter_kit.zip"))
    if kit_key:
        candidates = [kit_key] if kit_key in candidates else []
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one starter_kit.zip under s3://{bucket}/{prefix}, found {len(candidates)}. "
            "Check the artifact prefix; use AUTORAG_STARTER_KIT_S3_KEY to disambiguate. "
            "Older runs without the starter-kit output must be rerun with a current pipeline."
        )
    return candidates[0]


def extract_kit(archive: Path, destination: Path) -> Path:
    """Extract only a bounded, regular-file archive inside a private directory."""
    try:
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                raise RuntimeError("Starter-kit archive exceeds the supported size")
            for item in entries:
                path = PurePosixPath(item.filename)
                mode = item.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                        or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                        or any(part in {".env", ".git", ".aws", ".kube"} for part in path.parts)):
                    raise RuntimeError("Starter-kit archive contains an unsafe or credential-bearing path")
                target = destination.joinpath(*path.parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(item) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
        kit = destination / "starter_kit" if (destination / "starter_kit").is_dir() else destination
        required = {"README.md", "Makefile", "Containerfile.openshell", "agent_config.json", "values.yaml", "auth_wrapper.py"}
        if any(not (kit / name).is_file() for name in required):
            raise RuntimeError("Archive is not a supported OpenShell starter kit; check its README/version")
        return kit
    except (zipfile.BadZipFile, FileExistsError) as error:
        raise RuntimeError("Invalid starter-kit archive") from error


def kit_value(values: str, key: str) -> str:
    """Read a generated values.yaml scalar without imposing a pattern JSON schema."""
    match = re.search(rf"^\s*{re.escape(key)}:[ \t]*(.*)$", values, re.MULTILINE)
    if not match:
        return ""
    value = match[1].strip()
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise RuntimeError(f"Invalid generated values.yaml field: {key}") from error
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    else:
        value = value.split(" #", 1)[0].strip()
    if not isinstance(value, str):
        raise RuntimeError(f"Invalid generated values.yaml field: {key}")
    return "" if value in {"", "null", "~"} or re.fullmatch(r"__[A-Z0-9_]+__", value) else value


def prepare_runtime(kit: Path) -> None:
    """Use one pinned downstream runtime with the run's optimized application."""
    source = Path(__file__).with_name("autorag_runtime")
    names = ("Makefile", "Containerfile.openshell", "proxychains_entrypoint.py", "README.md")
    # Keep the run's originals outside the binary image build context.
    backup = kit.parent.parent / "original-runtime"
    backup.mkdir(exist_ok=True)
    pattern = kit_value((kit / "values.yaml").read_text(), "nameOverride") or "selected pattern"
    for name in names:
        target = kit / name
        if target.exists() and not (backup / name).exists():
            shutil.copyfile(target, backup / name)
        content = (source / name).read_text()
        if name == "README.md":
            content = content.replace("__PATTERN_NAME__", pattern)
        target.write_text(content)
    shutil.copyfile(source / "PROVENANCE.md", backup / "PROVENANCE.md")


def configure_kit(kit: Path, settings: dict) -> tuple[str, str, str]:
    """Use the generated kit defaults, filling missing connections or explicit overrides."""
    agent = json.loads((kit / "agent_config.json").read_text())
    if not isinstance(agent, dict):
        raise RuntimeError("Starter-kit agent_config.json must contain an object")
    if agent.get("template_id") != "agentic_rag":
        raise RuntimeError("Only agentic_rag starter kits are supported by the downstream deployment runtime")
    for section, key in (("generation", "model_id"), ("embedding", "model_id"),
                         ("vector_store", "provider_type"), ("vector_store", "collection_name")):
        value = (agent.get(section) or {}).get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Starter-kit agent_config.json requires {section}.{key}")
    if re.search(r"__[A-Z][A-Z0-9_]*__", json.dumps(agent)):
        raise RuntimeError("Starter-kit agent_config.json still contains unfilled placeholders")
    provider = agent["vector_store"]["provider_type"]
    if provider not in {"milvus", "pgvector", "neo4j"}:
        raise RuntimeError(f"Unsupported starter-kit vector-store provider: {provider}")
    values_path = kit / "values.yaml"
    values = values_path.read_text()
    maas = settings.get("autorag_deploy_maas_secret") or kit_value(values, "maas_secret_name") or settings["maas_secret"]
    database = settings.get("autorag_deploy_db_secret") or kit_value(values, "db_secret_name") or settings["vector_db_secret"]
    for key, name in (("maas_secret_name", maas), ("db_secret_name", database)):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name):
            raise RuntimeError(f"Invalid Kubernetes connection name for {key}")
        if kit_value(values, key) == name:
            continue
        expression = rf"^([ \t]*){key}:[^\n]*$"
        if re.search(expression, values, re.MULTILINE):
            values = re.sub(expression, lambda match: f"{match[1]}{key}: {json.dumps(name)}", values, flags=re.MULTILINE)
        elif re.search(r"^secrets:[ \t]*$", values, re.MULTILINE):
            values = re.sub(r"^secrets:[ \t]*$", lambda _: f"secrets:\n  {key}: {json.dumps(name)}", values, count=1, flags=re.MULTILINE)
        else:
            values += f"\nsecrets:\n  {key}: {json.dumps(name)}\n"
    if values != values_path.read_text():
        values_path.write_text(values)
    prepare_runtime(kit)
    print("  Using ai4rag v0.25.4 downstream deployment templates and RHOAI sandbox/CLI images.")
    return provider, maas, database


def write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace a credential file with owner-only permissions."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(payload, output, indent=2)
            output.write("\n")
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def connect_app(settings: dict, endpoint: str, token: str) -> None:
    """Merge guidance credentials without changing existing scoring settings."""
    path = settings["app_config_path"]
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise RuntimeError("App configuration must contain a JSON object")
    for field in ("endpoints", "endpoint_tokens", "endpoint_tls_verify", "endpoint_ca_bundles", "endpoint_tls_strict"):
        if field in config and not isinstance(config[field], dict):
            raise RuntimeError(f"App configuration {field} must contain a JSON object")
    config.setdefault("endpoints", {})["responses"] = endpoint
    config.setdefault("endpoint_tokens", {})["responses"] = token
    config.setdefault("endpoint_tls_verify", {})["responses"] = settings["app_endpoint_verify_ssl"]
    config.setdefault("endpoint_tls_strict", {})["responses"] = settings.get("app_endpoint_tls_strict", True)
    if settings["app_endpoint_ca_bundle"]:
        config.setdefault("endpoint_ca_bundles", {})["responses"] = settings["app_endpoint_ca_bundle"]
    else:
        config.get("endpoint_ca_bundles", {}).pop("responses", None)
    if not isinstance(config.get("api_token"), str) or not config["api_token"].strip():
        config["api_token"] = settings["token"]
    config["responses_auth_header"] = "X-Api-Key"
    config.pop("responses_protocol", None)
    config.pop("responses_template", None)
    write_private_json(path, config)


class KitCommands:
    """Run the archive commands with a dedicated kubeconfig and private log."""

    def __init__(self, settings: dict, kit: Path, workspace: Path):
        self.kit = kit
        self.timeout = settings["autorag_deploy_timeout"]
        self.log = workspace / "deployment.log"
        self.log.touch(mode=0o600)
        self.environment = dict(os.environ)
        self.environment["PATH"] = cluster_tool_path()
        # Avoid shell evaluation of connection values inherited from the demo .env.
        for name in list(self.environment):
            if name.startswith(("AWS_", "MAAS_", "MILVUS_", "PGVECTOR_", "NEO4J_")) or name in {
                "MAKEFLAGS", "MFLAGS", "MAKEFILES", "RHOAI_TOKEN", "DB_SECRET_NAME",
            }:
                self.environment.pop(name)
        kubeconfig = workspace / "kubeconfig.json"
        cluster = {"server": settings["rhoai_url"], "insecure-skip-tls-verify": not settings["openshift_verify_ssl"]}
        if settings.get("openshift_ca_bundle"):
            cluster["certificate-authority"] = str(Path(settings["openshift_ca_bundle"]).resolve())
        write_private_json(kubeconfig, {
            "apiVersion": "v1", "kind": "Config", "current-context": "city-services",
            "clusters": [{"name": "cluster", "cluster": cluster}],
            "users": [{"name": "runner", "user": {"token": settings["token"]}}],
            "contexts": [{"name": "city-services", "context": {
                "cluster": "cluster", "user": "runner", "namespace": settings["project"],
            }}],
        })
        self.environment.update(KUBECONFIG=str(kubeconfig),
                                OPENSHELL_SANDBOX_NAME=settings["autorag_sandbox_name"])
        self.environment.setdefault("OPENSHELL_GATEWAY_INSECURE", "false")
        self.kubeconfig = kubeconfig
        # Use the kit's CSV discovery, image fallback, state mount and mTLS.
        (kit / "city-services-cli.mk").write_text(
            ".PHONY: city-services-cli-status\n"
            "city-services-cli-status:\n"
            "\t@$(_openshell-cli) && $(OPENSHELL) status --output json\n"
        )

    def run(self, command: list[str], *, capture: bool = False) -> str:
        with self.log.open("a") as output:
            output.write("$ " + " ".join(command) + "\n")
            output.flush()
            try:
                result = subprocess.run(command, cwd=self.kit, env=self.environment, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE if capture else output, stderr=output,
                                        text=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"{command[0]} timed out; private deployment log: {self.log}") from error
        if result.returncode:
            raise RuntimeError(f"{command[0]} failed (exit {result.returncode}); private deployment log: {self.log}")
        return result.stdout if capture else ""

    def oc_json(self, *args: str) -> dict:
        return json.loads(self.run(["oc", *args, "-o", "json"], capture=True))

    def secret(self, name: str) -> dict[str, str]:
        data = self.oc_json("get", "secret", name).get("data", {})
        return {key: base64.b64decode(value, validate=True).decode() for key, value in data.items()}


def check_connections(commands: KitCommands, provider: str, maas: str, database: str) -> None:
    for name, required in ((maas, ("MAAS_BASE_URL", "MAAS_API_KEY")), (database, {
        "milvus": ("MILVUS_URI",), "pgvector": ("PGVECTOR_HOST",), "neo4j": ("NEO4J_URI", "NEO4J_PASSWORD"),
    }[provider])):
        connection = commands.secret(name)
        missing = [key for key in required if not connection.get(key, "").strip()]
        if missing:
            raise RuntimeError(f"Existing connection {name!r} is missing: {', '.join(missing)}")


def build_pull_secret(commands: KitCommands, requested: str = "") -> str:
    """Select existing project registry credentials for Docker multi-stage pulls."""
    if requested:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", requested):
            raise RuntimeError("Invalid AUTORAG_BUILD_PULL_SECRET_NAME")
        candidates = [requested]
    else:
        builder = commands.oc_json("get", "serviceaccount", "builder")
        candidates = [item["name"] for item in builder.get("imagePullSecrets", [])]
    for name in candidates:
        data = commands.secret(name)
        try:
            config = json.loads(data.get(".dockerconfigjson") or data.get(".dockercfg") or "{}")
            auths = config.get("auths", config)
            hosts = {host.removeprefix("https://").removeprefix("http://").split("/")[0]
                     for host, credentials in auths.items() if credentials}
        except (ValueError, AttributeError) as error:
            raise RuntimeError(f"Registry secret {name!r} contains invalid Docker configuration") from error
        if requested or hosts & {"registry.redhat.io", "quay.io"}:
            if not hosts:
                raise RuntimeError(f"Registry secret {name!r} has no Docker registry credentials")
            return name
    return ""


def check_gateway(commands: KitCommands) -> None:
    """Require a usable gateway; OpenShell status can exit zero when unconfigured."""
    try:
        output = commands.run(["make", "--no-print-directory", "-s", "-f", "Makefile",
                               "-f", "city-services-cli.mk", "city-services-cli-status"], capture=True)
        # CLI diagnostics can precede the JSON status object.
        output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        start = re.search(r"(?m)^[ \t]*\{", output)
        status = json.loads(output[start.start():] if start else output)
    except ValueError as error:
        raise RuntimeError("OpenShell returned invalid gateway status JSON; inspect the private deployment log") from error
    if not isinstance(status, dict) or status.get("status") != "connected":
        raise RuntimeError(
            "OpenShell has no connected active gateway. This deployment requires a preinstalled "
            "Red Hat cluster gateway. Run make refresh-certs in the prepared kit to refresh "
            "mTLS certificates and register openshift, then check gateway readiness. "
            "The image build and sandbox deployment were not started."
        )
    if (status.get("authentication") or {}).get("status") not in {"authenticated", "not_required"}:
        raise RuntimeError(
            "OpenShell gateway authentication is not ready; inspect openshell status before deploying. "
            "For mTLS gateways, use the registered client certificates and unset OPENSHELL_GATEWAY_INSECURE; "
            "insecure TLS can disable client-certificate authentication."
        )


def endpoint_settings(settings: dict, commands: KitCommands, workspace: Path) -> dict:
    """Trust the kit gateway's CA for its passthrough agent Route."""
    if not settings["app_endpoint_verify_ssl"] or settings["app_endpoint_ca_bundle"]:
        return settings
    secret = commands.oc_json("-n", "openshell", "get", "secret", "openshell-client-tls")
    try:
        certificate = base64.b64decode(secret["data"]["ca.crt"], validate=True)
        if b"-----BEGIN CERTIFICATE-----" not in certificate:
            raise ValueError("missing PEM certificate")
    except (KeyError, ValueError) as error:
        raise RuntimeError("OpenShell gateway secret has no valid CA certificate") from error
    path = workspace / "agent-ca.pem"
    with path.open("wb") as output:
        os.chmod(path, 0o600)
        output.write(certificate)
    # The generated gateway certificates omit AKI. Match Python 3.12's
    # validation on 3.13+, retaining trusted-chain, hostname and expiry checks.
    return dict(settings, app_endpoint_ca_bundle=str(path.resolve()), app_endpoint_tls_strict=False)


def verify_endpoint(settings: dict, base_url: str, token: str) -> None:
    """Wait for health, then require a real authenticated Responses answer."""
    context = (ssl.create_default_context(cafile=settings["app_endpoint_ca_bundle"] or None)
               if settings["app_endpoint_verify_ssl"] else ssl._create_unverified_context())
    if not settings.get("app_endpoint_tls_strict", True):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    deadline = time.monotonic() + settings["autorag_health_timeout"]
    while True:
        try:
            with urlopen(base_url + "/health", context=context, timeout=10) as response:
                if response.status == 200:
                    break
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise RuntimeError("Agent TLS certificate verification failed; check APP_ENDPOINT_CA_BUNDLE") from error
        except TimeoutError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent health check timed out; inspect the private deployment log and Route/TLS settings")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    request = Request(base_url + "/v1/responses", method="POST", headers={
        "X-Api-Key": token, "Content-Type": "application/json",
    }, data=json.dumps({"input": settings["autorag_smoke_question"], "stream": False}).encode())
    try:
        with urlopen(request, context=context, timeout=settings["autorag_health_timeout"]) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("Agent invocation returned an invalid response object; app configuration was not changed")
        answer = payload.get("output_text") or next((
            item.get("text") for message in (payload.get("output") or []) if isinstance(message, dict)
            for item in (message.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "output_text" and item.get("text")
        ), "")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Agent invocation returned no answer; app configuration was not changed")
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        code = f" (HTTP {error.code})" if isinstance(error, HTTPError) else ""
        raise RuntimeError(f"Authenticated agent invocation failed{code}; app configuration was not changed") from error


def wait_for_build(commands: KitCommands, name: str, timeout: int) -> None:
    """Use the Build status as authority, even if the build log stream ends early."""
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        status = commands.oc_json("get", "build", name).get("status", {})
        phase = status.get("phase", "Unknown")
        if phase == "Complete":
            return
        if phase in {"Failed", "Error", "Cancelled"}:
            raise RuntimeError(
                f"OpenShift image build did not complete ({name}: {phase}); "
                f"inspect oc logs build/{name}. App configuration was not changed."
            )
        if phase != previous:
            print(f"  Build {name}: {phase}; waiting for completion…", flush=True)
            previous = phase
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Timed out waiting for OpenShift build {name} ({phase}). "
                "The build was not cancelled; check its status before starting another deployment."
            )
        time.sleep(min(5, remaining))


def deploy_starter_kit(settings: dict, run_id: str, client, prefix: str, *, dry_run: bool) -> None:
    """Download, configure, build, deploy, verify, and connect the run's best kit."""
    if not settings["rhoai_url"]:
        raise RuntimeError("AutoRAG deployment requires RHOAI_URL")
    bucket = settings["artifact_bucket"]
    kit_key = find_artifacts(client, bucket, prefix, settings["autorag_starter_kit_key"])
    print(f"  Starter kit: s3://{bucket}/{kit_key}")
    if dry_run:
        workspace_context = tempfile.TemporaryDirectory(prefix="city-services-autorag-")
        workspace = Path(workspace_context.name)
    else:
        settings["autorag_work_dir"].mkdir(parents=True, exist_ok=True)
        workspace_context = None
        workspace = Path(tempfile.mkdtemp(prefix="deployment-", dir=settings["autorag_work_dir"]))
    try:
        archive = workspace / "starter_kit.zip"
        response = client.get_object(Bucket=bucket, Key=kit_key)
        if response.get("ContentLength", 0) > 100 * 1024 * 1024:
            raise RuntimeError("Starter-kit archive exceeds the supported size")
        with response["Body"] as body:
            content = body.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            raise RuntimeError("Starter-kit archive exceeds the supported size")
        archive.write_bytes(content)
        kit = extract_kit(archive, workspace / "extracted")
        provider, maas, database = configure_kit(kit, settings)
        pattern_name = kit_value((kit / "values.yaml").read_text(), "nameOverride")
        print(f"  Generated kit defaults: {pattern_name or 'selected pattern'}")
        write_private_json(workspace / "artifacts.json", {
            "run_id": run_id, "pattern_name": pattern_name, "starter_kit_uri": f"s3://{bucket}/{kit_key}",
        })
        print(f"  Connections: MaaS={maas}, database={database} ({provider}); existing indexed collection")
        print("  Gateway: refresh mTLS/registration and validate the RHOAI containerized CLI connection")
        print("  Plan: gateway validation → uv lock → OpenShift image build/push → OpenShell deploy → authenticated test → app config")
        if dry_run:
            print(f"  App config: {settings['app_config_path']}")
            return
        prerequisites = ["oc", "podman", "uv", "make", "curl", "python3"]
        missing = [name for name in prerequisites
                   if not shutil.which(name, path=cluster_tool_path())]
        if missing:
            raise RuntimeError("Install starter-kit prerequisites: " + ", ".join(missing))
        commands = KitCommands(settings, kit, workspace)
        print(f"  Private deployment files/log: {workspace}", flush=True)
        check_connections(commands, provider, maas, database)
        try:
            commands.oc_json("-n", "openshell", "get", "statefulset", "openshell")
            commands.run(["oc", "-n", "openshell", "rollout", "status", "statefulset/openshell", "--timeout=120s"])
        except RuntimeError as error:
            raise RuntimeError(
                "A ready OpenShell gateway is required in the openshell namespace. "
                "Check its installation, readiness, and your access permissions before retrying deployment. "
                f"{error}"
            ) from error
        print("  Refreshing gateway mTLS certificates and registration using the kit's RHOAI CLI…", flush=True)
        commands.run(["make", "refresh-certs"])
        check_gateway(commands)
        pull_secret = build_pull_secret(commands, settings.get("autorag_build_pull_secret", ""))
        if pull_secret:
            print(f"  Build registry credentials: existing project secret {pull_secret}", flush=True)
        print("  Updating the kit's ai4rag dependency lockfile…", flush=True)
        commands.run(["uv", "lock", "--upgrade-package", "ai4rag"])
        image = "city-services-rag"
        print("  Building and pushing the agent image inside OpenShift…", flush=True)
        build_command = ["make", f"OPENSHELL_IMAGE={image}"]
        if pull_secret:
            build_command.append(f"OPENSHELL_BUILD_PULL_SECRET={pull_secret}")
        commands.run([*build_command, "build-openshell"])
        build_number = commands.oc_json("get", "bc", image)["status"]["lastVersion"]
        wait_for_build(commands, f"{image}-{build_number}", settings["autorag_deploy_timeout"])
        print("  Deploying the OpenShell sandbox and agent Route…", flush=True)
        commands.run(["make", f"OPENSHELL_IMAGE={image}", "deploy-openshell"])
        route = commands.oc_json("-n", "openshell", "get", "route", "openshell-rag-agent")
        host = route.get("spec", {}).get("host", "")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            raise RuntimeError("Deployed agent Route has no valid host")
        token = commands.secret("agent-client-token").get("token", "").strip()
        if not token:
            raise RuntimeError("Starter-kit deployment did not produce an agent-client-token")
        base_url = "https://" + host
        app_settings = endpoint_settings(settings, commands, workspace)
        print("  Checking agent health and authenticated guidance…", flush=True)
        verify_endpoint(app_settings, base_url, token)
        connect_app(app_settings, base_url + "/v1/responses", token)
        print(f"  Guidance endpoint: {base_url}/v1/responses")
        print(f"  URL and token saved privately to {settings['app_config_path']}. Restart python3 main.py.")
        print("  The kit requests 168-hour service-account tokens (cluster policy may shorten this); redeploy before expiry.")
    finally:
        (workspace / "kubeconfig.json").unlink(missing_ok=True)
        if workspace_context:
            workspace_context.cleanup()
