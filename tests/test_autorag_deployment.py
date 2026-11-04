"""Exercise starter-kit deployment boundaries without credentials or a cluster."""
import base64
import io
import json
from pathlib import Path
import stat
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy_autorag as deployment
import run_cluster_demo as runner
import setup_demo as setup


PREFIX = "documents-rag-optimization-pipeline/run-1/"
TASK = PREFIX + "optimize/task/"
PATTERN_KEY = TASK + "rag_patterns/best/pattern.json"
KIT_KEY = TASK + "starter_kit/starter_kit.zip"


def kit_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("README.md", "Containerfile.openshell", "auth_wrapper.py"):
            archive.writestr("starter_kit/" + name, "generated file")
        archive.writestr("starter_kit/values.yaml", 'nameOverride: "best"\n\nsecrets:\n  maas_secret_name: "run-maas"\n  db_secret_name: "run-db"\n')
        archive.writestr("starter_kit/Makefile", "generated deployment file")
        archive.writestr("starter_kit/agent_config.json", json.dumps({
            "template_id": "agentic_rag", "generation": {"model_id": "chat-model", "temperature": 0.15},
            "embedding": {"model_id": "embedding-model"},
            "vector_store": {"provider_type": "milvus", "collection_name": "indexed-city-collection"},
            "prompts": {"system_message": "Preserve this generated prompt."},
        }))
    return buffer.getvalue()


def artifact_client(extra=None):
    objects = {KIT_KEY: kit_bytes()}
    objects.update(extra or {})
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": key} for key in objects]}]
    client.get_object.side_effect = lambda Bucket, Key: {"Body": io.BytesIO(objects[Key]), "ContentLength": len(objects[Key])}
    return client


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.settings = {
            "rhoai_url": "https://api.example:6443", "token": "private-cluster-token", "project": "city",
            "artifact_bucket": "artifacts", "autorag_starter_kit_key": "", "autorag_work_dir": self.root / "work",
            "autorag_sandbox_name": "city-services-rag", "autorag_deploy_timeout": 1800, "autorag_health_timeout": 1,
            "autorag_smoke_question": "Where do I report dumping?", "maas_secret": "fallback-maas", "vector_db_secret": "fallback-db",
            "app_config_path": self.root / "app-config.json", "app_endpoint_verify_ssl": True, "app_endpoint_ca_bundle": "",
            "openshift_verify_ssl": True,
        }

    def test_finds_run_level_kit_without_reading_pattern_artifacts(self):
        client = artifact_client({PATTERN_KEY: b"unrelated pattern schema"})
        archive = deployment.find_artifacts(client, "artifacts", PREFIX)
        self.assertEqual(archive, KIT_KEY)
        client.get_object.assert_not_called()

    def test_missing_or_ambiguous_archive_fails(self):
        client = artifact_client({TASK + "alternate/starter_kit.zip": b"other"})
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            deployment.find_artifacts(client, "artifacts", PREFIX)
        self.assertEqual(deployment.find_artifacts(client, "artifacts", PREFIX, KIT_KEY), KIT_KEY)
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            deployment.find_artifacts(client, "artifacts", PREFIX, "outside/run/starter_kit.zip")

    def test_kit_can_be_stored_above_pattern_task_artifacts(self):
        client = Mock()
        key = PREFIX + "starter_kit/starter_kit.zip"
        client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": key}, {"Key": PATTERN_KEY}]}]
        self.assertEqual(deployment.find_artifacts(client, "artifacts", PREFIX), key)
        client.get_object.assert_not_called()

    def test_unsafe_archive_paths_and_symlinks_are_rejected(self):
        for name in ("../escape", "/absolute", "starter_kit/.env", "starter_kit/.kube/config", "starter_kit\\escape"):
            archive = self.root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(name, "bad")
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                deployment.extract_kit(archive, self.root / "extract")
        with zipfile.ZipFile(archive, "w") as zipped:
            item = zipfile.ZipInfo("starter_kit/link")
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(item, "/etc/passwd")
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            deployment.extract_kit(archive, self.root / "extract")

    def test_configuration_keeps_generated_settings_and_uses_run_connections(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        kit = deployment.extract_kit(archive, self.root / "extract")
        before = (kit / "agent_config.json").read_bytes()
        self.assertEqual(deployment.configure_kit(kit, self.settings), ("milvus", "run-maas", "run-db"))
        self.assertEqual((kit / "agent_config.json").read_bytes(), before)
        self.assertIn('maas_secret_name: "run-maas"', (kit / "values.yaml").read_text())

    def test_pinned_runtime_preserves_pattern_and_backs_up_deployment_files(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        kit = deployment.extract_kit(archive, self.root / "extract")
        (kit / "Containerfile.openshell").write_text(
            "ARG BASE_IMAGE=ghcr.io/nvidia/openshell-community/sandboxes/base@sha256:old\n"
        )
        before = {name: (kit / name).read_bytes() for name in ("agent_config.json", "values.yaml")}
        self.assertEqual(deployment.configure_kit(kit, self.settings), ("milvus", "run-maas", "run-db"))
        for name, content in before.items():
            self.assertEqual((kit / name).read_bytes(), content)
        makefile = (kit / "Makefile").read_text()
        self.assertIn("odh_openshell_sandbox_image", makefile)
        self.assertIn("odh_openshell_cli_image", makefile)
        self.assertIn("export OPENSHELL_GRPC_PROXY_PORT=18080", makefile)
        self.assertNotIn("city-services-bootstrap-policy.yaml", makefile)
        self.assertFalse((kit / "city-services-bootstrap-policy.yaml").exists())
        self.assertIn(" AS builder", (kit / "Containerfile.openshell").read_text())
        self.assertIn("class ConnectProxy", (kit / "proxychains_entrypoint.py").read_text())
        self.assertIn("`best`", (kit / "README.md").read_text())
        backup = kit.parent.parent / "original-runtime"
        self.assertIn("@sha256:old", (backup / "Containerfile.openshell").read_text())
        deployed = (kit / "Makefile").read_bytes()
        deployment.prepare_runtime(kit)
        self.assertEqual((kit / "Makefile").read_bytes(), deployed)
        self.assertIn("@sha256:old", (backup / "Containerfile.openshell").read_text())
        result = subprocess.run(["make", "-n", "start-agent"], cwd=kit, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export OPENSHELL_GRPC_PROXY_PORT=18080", result.stdout)

    def test_gateway_preflight_uses_the_same_downstream_cli_as_the_kit(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        kit = deployment.extract_kit(archive, self.root / "extract")
        (kit / "Makefile").write_text((Path(deployment.__file__).with_name("autorag_runtime") / "Makefile").read_text())
        commands = deployment.KitCommands(self.settings, kit, self.root)
        response = json.dumps({"status": "connected", "authentication": {"status": "authenticated"}})
        with patch.object(deployment.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=response)) as run:
            deployment.check_gateway(commands)
        self.assertEqual(run.call_args.args[0][-1], "city-services-cli-status")
        self.assertEqual(run.call_args.args[0][0], "make")
        self.assertIn("$(_openshell-cli) && $(OPENSHELL) status", (kit / "city-services-cli.mk").read_text())
        self.assertIn('nameOverride: "best"', (kit / "values.yaml").read_text())

    def test_unsupported_template_is_rejected_before_runtime_changes(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        kit = deployment.extract_kit(archive, self.root / "extract")
        path = kit / "agent_config.json"
        agent = json.loads(path.read_text())
        agent.pop("template_id")
        path.write_text(json.dumps(agent))
        before = (kit / "Makefile").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Only agentic_rag starter kits"):
            deployment.configure_kit(kit, self.settings)
        self.assertEqual((kit / "Makefile").read_bytes(), before)

    def test_missing_connection_defaults_fall_back_to_env_and_overrides_are_explicit(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        kit = deployment.extract_kit(archive, self.root / "extract")
        values = kit / "values.yaml"
        values.write_text('nameOverride: "best"\nsecrets:\n  maas_secret_name: "__MAAS_SECRET_NAME__"\n')
        self.assertEqual(deployment.configure_kit(kit, self.settings), ("milvus", "fallback-maas", "fallback-db"))
        self.settings["autorag_deploy_maas_secret"] = "override-maas"
        self.settings["autorag_deploy_db_secret"] = "override-db"
        self.assertEqual(deployment.configure_kit(kit, self.settings), ("milvus", "override-maas", "override-db"))
        self.assertIn('nameOverride: "best"', values.read_text())

    def test_app_merge_preserves_scoring_and_removes_bff_configuration(self):
        original = {"api_token": "original-token", "endpoint_protocol": "kserve-v1",
                    "endpoints": {"tabular_scoring": "https://tabular", "timeseries_scoring": "https://timeseries"},
                    "endpoint_tokens": {"tabular_scoring": "scoring-token"}, "responses_template": {"model": "old"},
                    "responses_protocol": "dashboard-bff", "tabular_feature_importance": {"features": []},
                    "endpoint_ca_bundles": {"responses": "old-ca", "tabular_scoring": "scoring-ca"}}
        path = self.settings["app_config_path"]
        path.write_text(json.dumps(original))
        deployment.connect_app(self.settings, "https://agent/v1/responses", "private-agent-token")
        updated = json.loads(path.read_text())
        for key in ("api_token", "endpoint_protocol", "tabular_feature_importance"):
            self.assertEqual(updated[key], original[key])
        self.assertEqual(updated["endpoints"]["tabular_scoring"], "https://tabular")
        self.assertEqual(updated["endpoint_tokens"]["tabular_scoring"], "scoring-token")
        self.assertEqual(updated["endpoint_tokens"]["responses"], "private-agent-token")
        self.assertEqual(updated["responses_auth_header"], "X-Api-Key")
        self.assertNotIn("responses_protocol", updated)
        self.assertNotIn("responses_template", updated)
        self.assertEqual(updated["endpoint_ca_bundles"], {"tabular_scoring": "scoring-ca"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def commands(self):
        commands = Mock()
        commands.run.return_value = json.dumps({"status": "connected", "authentication": {"status": "authenticated"}})
        commands.secret.side_effect = lambda name: {
            "run-maas": {"MAAS_BASE_URL": "https://maas", "MAAS_API_KEY": "secret"},
            "run-db": {"MILVUS_URI": "https://milvus:19530"},
            "agent-client-token": {"token": "private-agent-token"},
        }[name]
        commands.oc_json.side_effect = lambda *args: (
            {"status": {"lastVersion": 3}} if "bc" in args else
            {"status": {"phase": "Complete"}} if "build" in args else
            {"spec": {"host": "agent.example"}} if "route" in args else
            {"data": {"ca.crt": base64.b64encode(b"-----BEGIN CERTIFICATE-----\ntest\n").decode()}} if "secret" in args else {}
        )
        return commands

    def command_factory(self, commands):
        def create(settings, kit, workspace):
            (workspace / "kubeconfig.json").write_text("private kubeconfig")
            return commands
        return create

    def test_full_flow_builds_deploys_checks_and_then_connects(self):
        commands = self.commands()
        events = []
        with patch.object(deployment, "KitCommands", side_effect=self.command_factory(commands)), patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment, "verify_endpoint", side_effect=lambda *a: events.append("verified")), \
                patch.object(deployment, "connect_app", side_effect=lambda *a: events.append("connected")) as connect:
            deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        calls = [call.args[0] for call in commands.run.call_args_list]
        self.assertEqual(calls, [
                                 ["oc", "-n", "openshell", "rollout", "status", "statefulset/openshell", "--timeout=120s"],
                                 ["make", "refresh-certs"],
                                 ["make", "--no-print-directory", "-s", "-f", "Makefile", "-f", "city-services-cli.mk", "city-services-cli-status"],
                                 ["uv", "lock", "--upgrade-package", "ai4rag"],
                                 ["make", "OPENSHELL_IMAGE=city-services-rag", "build-openshell"],
                                 ["make", "OPENSHELL_IMAGE=city-services-rag", "deploy-openshell"]])
        self.assertEqual(events, ["verified", "connected"])
        self.assertEqual(connect.call_args.args[1:], ("https://agent.example/v1/responses", "private-agent-token"))
        self.assertEqual(list(self.settings["autorag_work_dir"].rglob("kubeconfig.json")), [])

    def test_failed_invocation_keeps_app_configuration_and_cleans_kubeconfig(self):
        commands = self.commands()
        path = self.settings["app_config_path"]
        path.write_text('{"api_token":"existing","endpoints":{}}')
        original = path.read_bytes()
        with patch.object(deployment, "KitCommands", side_effect=self.command_factory(commands)), patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment, "verify_endpoint", side_effect=RuntimeError("auth failed")):
            with self.assertRaisesRegex(RuntimeError, "auth failed"):
                deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.settings["autorag_work_dir"].rglob("kubeconfig.json")), [])

    def test_deployment_requires_podman_instead_of_a_local_cli(self):
        commands = self.commands()
        client = artifact_client({KIT_KEY: kit_bytes()})
        def installed(name, **kwargs):
            return None if name in {"openshell"} else "installed"
        with patch.object(deployment, "KitCommands", side_effect=self.command_factory(commands)), \
                patch.object(deployment.shutil, "which", side_effect=installed), \
                patch.object(deployment, "verify_endpoint") as verify, patch.object(deployment, "connect_app") as connect:
            deployment.deploy_starter_kit(self.settings, "run-1", client, PREFIX, dry_run=False)
        calls = [call.args[0] for call in commands.run.call_args_list]
        self.assertIn(["make", "refresh-certs"], calls)
        self.assertIn(["make", "OPENSHELL_IMAGE=city-services-rag", "deploy-openshell"], calls)
        verify.assert_called_once()
        connect.assert_called_once()

    def test_missing_podman_stops_before_cluster_commands(self):
        with patch.object(deployment.shutil, "which", side_effect=lambda name, **kw: None if name == "podman" else "installed"), \
                patch.object(deployment, "KitCommands") as commands:
            with self.assertRaisesRegex(RuntimeError, "prerequisites: podman"):
                deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        commands.assert_not_called()

    def test_build_selects_existing_external_registry_credentials(self):
        commands = Mock()
        commands.oc_json.return_value = {"imagePullSecrets": [{"name": "internal"}, {"name": "downstream"}]}
        commands.secret.side_effect = lambda name: {".dockerconfigjson": json.dumps({"auths": {
            "image-registry.openshift.svc:5000" if name == "internal" else "quay.io": {"auth": "private"},
        }})}
        self.assertEqual(deployment.build_pull_secret(commands), "downstream")
        commands.run.assert_not_called()
        self.assertEqual(commands.secret.call_count, 2)

    def test_explicit_build_registry_secret_is_validated(self):
        commands = Mock()
        commands.secret.return_value = {".dockerconfigjson": '{"auths":{"registry.redhat.io":{"auth":"private"}}}'}
        self.assertEqual(deployment.build_pull_secret(commands, "requested"), "requested")
        commands.oc_json.assert_not_called()
        commands.secret.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "no Docker registry credentials"):
            deployment.build_pull_secret(commands, "requested")
        with self.assertRaisesRegex(RuntimeError, "Invalid AUTORAG_BUILD_PULL_SECRET_NAME"):
            deployment.build_pull_secret(commands, "bad/name")

    def test_selected_registry_secret_is_passed_to_image_build(self):
        commands = self.commands()
        with patch.object(deployment, "KitCommands", return_value=commands), \
                patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment, "build_pull_secret", return_value="existing-registry"), \
                patch.object(deployment, "verify_endpoint"), patch.object(deployment, "connect_app"):
            deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        commands.run.assert_any_call(["make", "OPENSHELL_IMAGE=city-services-rag",
                                      "OPENSHELL_BUILD_PULL_SECRET=existing-registry", "build-openshell"])

    def test_gateway_install_flag_is_rejected_by_both_entry_points(self):
        for entry in (setup, runner):
            with self.subTest(entry=entry.__name__), \
                    patch.object(sys, "argv", [entry.__file__, "--setup-autorag-gateway"]), \
                    patch.object(sys, "stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as stopped:
                    entry.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_current_kit_missing_gateway_connection_stops_before_build(self):
        commands = self.commands()
        commands.run.return_value = '{"status":"not_configured"}'
        with patch.object(deployment, "KitCommands", return_value=commands), \
                patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment, "connect_app") as connect:
            with self.assertRaisesRegex(RuntimeError, "requires a preinstalled") as error:
                deployment.deploy_starter_kit(self.settings, "run-1",
                    artifact_client({KIT_KEY: kit_bytes()}), PREFIX,
                    dry_run=False)
        self.assertFalse(any("build-openshell" in call.args[0] or call.args[0][0] == "uv"
                             for call in commands.run.call_args_list))
        connect.assert_not_called()

    def test_missing_cluster_gateway_stops_before_build_with_installation_guidance(self):
        commands = self.commands()
        commands.oc_json.side_effect = RuntimeError("oc failed; private deployment log")
        with patch.object(deployment, "KitCommands", return_value=commands), \
                patch.object(deployment.shutil, "which", return_value="installed"):
            with self.assertRaisesRegex(RuntimeError, "ready OpenShell gateway is required"):
                deployment.deploy_starter_kit(self.settings, "run-1",
                    artifact_client({KIT_KEY: kit_bytes()}), PREFIX,
                    dry_run=False)
        commands.run.assert_not_called()

    def test_deployment_uses_generated_kit_without_pattern_lookup(self):
        client = artifact_client({PATTERN_KEY: b"unsupported pattern format"})
        with patch.object(deployment, "KitCommands") as commands:
            deployment.deploy_starter_kit(self.settings, "run-1", client, PREFIX, dry_run=True)
        commands.assert_not_called()
        self.assertEqual([call.kwargs["Key"] for call in client.get_object.call_args_list], [KIT_KEY])

    def test_failed_build_does_not_deploy_or_connect(self):
        commands = self.commands()
        original = commands.oc_json.side_effect
        commands.oc_json.side_effect = lambda *args: {"status": {"phase": "Failed"}} if "build" in args else original(*args)
        with patch.object(deployment, "KitCommands", return_value=commands), patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment, "connect_app") as connect:
            with self.assertRaisesRegex(RuntimeError, "build did not complete"):
                deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        self.assertFalse(any("deploy-openshell" in call.args[0] for call in commands.run.call_args_list))
        connect.assert_not_called()

    def test_build_waits_after_log_stream_returns_while_build_is_running(self):
        commands = self.commands()
        original = commands.oc_json.side_effect
        phases = iter(["New", "Pending", "Running", "Complete"])
        commands.oc_json.side_effect = lambda *args: (
            {"status": {"phase": next(phases)}} if "build" in args else original(*args)
        )
        with patch.object(deployment, "KitCommands", return_value=commands), \
                patch.object(deployment.shutil, "which", return_value="installed"), \
                patch.object(deployment.time, "sleep") as sleep, \
                patch.object(deployment, "verify_endpoint"), patch.object(deployment, "connect_app") as connect:
            deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX,
                                          dry_run=False)
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(any("deploy-openshell" in call.args[0] for call in commands.run.call_args_list))
        connect.assert_called_once()

    def test_build_timeout_keeps_running_build_and_app_configuration(self):
        commands = self.commands()
        commands.oc_json.return_value = {"status": {"phase": "Running"}}
        commands.oc_json.side_effect = None
        with patch.object(deployment.time, "monotonic", side_effect=[0, 31]), \
                patch.object(deployment.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "build was not cancelled"):
                deployment.wait_for_build(commands, "city-services-rag-5", 30)
        sleep.assert_not_called()
        commands.run.assert_not_called()

    def test_terminal_build_failures_do_not_wait(self):
        for phase in ("Failed", "Error", "Cancelled"):
            with self.subTest(phase=phase), patch.object(deployment.time, "sleep") as sleep:
                commands = Mock()
                commands.oc_json.return_value = {"status": {"phase": phase}}
                with self.assertRaisesRegex(RuntimeError, f"city-services-rag-5: {phase}"):
                    deployment.wait_for_build(commands, "city-services-rag-5", 30)
                sleep.assert_not_called()

    def test_missing_connection_field_stops_before_build(self):
        commands = self.commands()
        commands.secret.side_effect = lambda name: {"MAAS_BASE_URL": "https://maas"}
        with patch.object(deployment, "KitCommands", return_value=commands), patch.object(deployment.shutil, "which", return_value="installed"):
            with self.assertRaisesRegex(RuntimeError, "missing: MAAS_API_KEY"):
                deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=False)
        commands.run.assert_not_called()

    def test_gateway_requires_grpc_and_successful_authentication(self):
        commands = self.commands()
        commands.run.return_value = '{"status":"connected_http","authentication":{"status":"authenticated"}}'
        with self.assertRaisesRegex(RuntimeError, "no connected active gateway"):
            deployment.check_gateway(commands)
        commands.run.return_value = '{"status":"connected","authentication":{"status":"failed"}}'
        with self.assertRaisesRegex(RuntimeError, "authentication is not ready"):
            deployment.check_gateway(commands)

    def test_gateway_accepts_real_cli_warning_before_json(self):
        commands = self.commands()
        commands.run.return_value = (
            '\x1b[2m2026-10-19T11:43:51Z\x1b[0m \x1b[33mWARN\x1b[0m openshell_cli::tls: '
            'Gateway diagnostic message\n'
            '{\n  "authentication": {"provider": "mTLS transport", "status": "authenticated"},\n'
            '  "gateway": "openshift", "status": "connected", "version": "0.1.6"\n}\n'
        )
        deployment.check_gateway(commands)

    def test_gateway_warning_does_not_mask_unconfigured_or_invalid_status(self):
        commands = self.commands()
        commands.run.return_value = 'WARN: gateway diagnostic\n{"status":"not_configured"}\n'
        with self.assertRaisesRegex(RuntimeError, "no connected active gateway"):
            deployment.check_gateway(commands)
        commands.run.return_value = 'WARN: gateway unavailable\n{invalid JSON}\n'
        with self.assertRaisesRegex(RuntimeError, "invalid gateway status JSON"):
            deployment.check_gateway(commands)

    def test_dry_run_reads_archive_without_commands_or_persistent_files(self):
        with patch.object(deployment, "KitCommands") as commands, patch.object(deployment, "connect_app") as connect:
            deployment.deploy_starter_kit(self.settings, "run-1", artifact_client(), PREFIX, dry_run=True)
        commands.assert_not_called()
        connect.assert_not_called()
        self.assertFalse(self.settings["autorag_work_dir"].exists())
        self.assertFalse(self.settings["app_config_path"].exists())

    def test_kubeconfig_is_isolated_from_build_context(self):
        workspace = self.root / "deployment"
        kit = workspace / "extracted" / "starter_kit"
        kit.mkdir(parents=True)
        commands = deployment.KitCommands(self.settings, kit, workspace)
        self.assertFalse(commands.kubeconfig.is_relative_to(kit))
        config = json.loads(commands.kubeconfig.read_text())
        self.assertEqual(config["contexts"][0]["context"]["namespace"], "city")
        self.assertEqual(stat.S_IMODE(commands.kubeconfig.stat().st_mode), 0o600)
        self.assertNotIn("RHOAI_TOKEN", commands.environment)
        self.assertEqual(commands.environment["OPENSHELL_GATEWAY_INSECURE"], "false")

    def test_runner_rejects_unsuccessful_run_before_reading_artifacts(self):
        client = Mock()
        client.get_run.return_value = types.SimpleNamespace(state="FAILED")
        with patch.object(runner, "artifact_client") as artifacts:
            with self.assertRaisesRegex(RuntimeError, "requires a successful run"):
                runner.deploy_autorag_run(self.settings, "run-1", dry_run=False, client=client)
        artifacts.assert_not_called()

    def test_noninteractive_auto_deploy_waits_and_deploys_autorag_only(self):
        args = types.SimpleNamespace(non_interactive=True, wait=False, auto_deploy_autorag=True,
                                     poll_interval=15)
        with patch.object(runner, "monitor_runs", return_value={"rag-run": "SUCCEEDED", "automl-run": "SUCCEEDED"}) as monitor, \
                patch.object(runner, "deploy_autorag_run") as deploy, patch.object(runner, "confirm") as confirm, \
                patch.object(runner, "deploy_automl_runs") as automl:
            runner.post_submit_guide(Mock(), self.settings, {"autorag": "rag-run", "tabular": "automl-run"}, args)
        monitor.assert_called_once()
        deploy.assert_called_once()
        deploy.assert_called_once_with(self.settings, "rag-run", dry_run=False, client=monitor.call_args.args[0])
        confirm.assert_not_called()
        automl.assert_not_called()

    def test_gateway_ca_is_saved_outside_build_context_and_applied_only_to_guidance(self):
        commands = self.commands()
        result = deployment.endpoint_settings(self.settings, commands, self.root)
        path = Path(result["app_endpoint_ca_bundle"])
        self.assertEqual(path.read_bytes(), b"-----BEGIN CERTIFICATE-----\ntest\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.settings["app_endpoint_ca_bundle"], "")
        self.assertTrue(result["app_endpoint_verify_ssl"])
        commands.oc_json.assert_called_once_with("-n", "openshell", "get", "secret", "openshell-client-tls")
        for overrides in ({"app_endpoint_ca_bundle": "explicit-ca.pem"}, {"app_endpoint_verify_ssl": False}):
            commands.reset_mock()
            settings = dict(self.settings, **overrides)
            self.assertIs(deployment.endpoint_settings(settings, commands, self.root), settings)
            commands.oc_json.assert_not_called()

    def test_certificate_errors_fail_immediately_without_health_retries(self):
        with patch.object(deployment, "urlopen", side_effect=URLError(ssl.SSLCertVerificationError("untrusted CA"))) as request, \
                patch.object(deployment.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "TLS certificate verification failed"):
                deployment.verify_endpoint(self.settings, "https://agent.example", "agent-token")
        request.assert_called_once()
        sleep.assert_not_called()

    def test_endpoint_requires_an_authenticated_nonempty_answer(self):
        class Response(io.BytesIO):
            status = 200
        seen = []
        def respond(request, **kwargs):
            seen.append(request)
            return Response(b'{"status":"ok"}' if isinstance(request, str) else b'{"output_text":"Contact 311."}')
        with patch.object(deployment, "urlopen", side_effect=respond):
            deployment.verify_endpoint(self.settings, "https://agent.example", "agent-token")
        request = seen[1]
        self.assertEqual(request.get_header("X-api-key"), "agent-token")
        self.assertEqual(json.loads(request.data)["stream"], False)
        with patch.object(deployment, "urlopen", side_effect=[Response(b"{}"), Response(b'{"output": []}')]):
            with self.assertRaisesRegex(RuntimeError, "no answer"):
                deployment.verify_endpoint(self.settings, "https://agent.example", "agent-token")

    def test_setup_forwards_deployment_flags_and_apply_skips_confirmation(self):
        env_file = self.root / ".env"
        env_file.touch()
        argv = ["setup_demo.py", "--env-file", str(env_file), "--deploy-autorag", "run-1", "--apply"]
        with patch.object(sys, "argv", argv), patch.object(setup, "run", return_value=0) as run, patch.object(setup, "confirm") as confirm:
            with self.assertRaises(SystemExit) as stopped:
                setup.main()
        self.assertEqual(stopped.exception.code, 0)
        preflight, execute = [call.args[0] for call in run.call_args_list]
        self.assertIn("--dry-run", preflight)
        self.assertNotIn("--dry-run", execute)
        self.assertIn("--non-interactive", execute)
        index = execute.index("--deploy-autorag")
        self.assertEqual(execute[index + 1], "run-1")
        confirm.assert_not_called()

    def test_setup_submission_passes_auto_deploy_to_preflight_and_runner(self):
        env_file = self.root / ".env"
        env_file.touch()
        argv = ["setup_demo.py", "--env-file", str(env_file), "--only", "autorag", "--auto-deploy-autorag", "--apply"]
        with patch.object(sys, "argv", argv), patch.object(setup, "run", return_value=0) as run:
            with self.assertRaises(SystemExit):
                setup.main()
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--auto-deploy-autorag", command)
            self.assertEqual(command[command.index("--only") + 1], "autorag")


if __name__ == "__main__":
    unittest.main()
