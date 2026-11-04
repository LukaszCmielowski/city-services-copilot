"""Check complete AutoML deployment and unattended setup without a cluster."""
import io
import json
from pathlib import Path
import stat
import ssl
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_cluster_demo as runner
import setup_demo as setup


class ApiException(Exception):
    def __init__(self, status):
        self.status = status


class AutoMLDeploymentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = {
            "token": "cluster-token", "app_config_path": self.root / "app-config.json",
            "app_endpoint_verify_ssl": True, "app_endpoint_ca_bundle": "",
            "tabular_pipeline": "tabular", "timeseries_pipeline": "timeseries", "project": "demo",
            "serving_runtime": "existing", "kserve_storage_key": "s3", "kserve_service_account": "",
            "deploy_cpu": "2", "deploy_memory": "4Gi",
            "automl_work_dir": self.root / "work",
        }
        self.pipeline = Mock()
        self.pipeline.get_run.return_value = types.SimpleNamespace(state="SUCCEEDED")
        self.cluster = Mock()
        self.cluster.get_namespaced_custom_object.return_value = {
            "status": {"url": "https://model.example", "conditions": [{"type": "Ready", "status": "True"}]}}
        patches = [
            patch("builtins.print"),
            patch("ssl.create_default_context", return_value=ssl.create_default_context(cafile=str(Path(__file__).parent / "fixtures/test-ca.pem"))),
            patch.dict(sys.modules, {"kubernetes.client.rest": types.SimpleNamespace(ApiException=ApiException)}),
            patch.object(runner, "make_client", return_value=self.pipeline),
            patch.object(runner, "kserve_client", return_value=self.cluster),
            patch.object(runner, "runtime_for_deployment", return_value="existing"),
            patch.object(runner, "select_best_automl_model", side_effect=lambda settings, run: {
                "pipeline": run, "model": "best-" + run, "metric": "score_val", "score": 0.8,
                "predictor_prefix": run + "/predictor/"}),
            patch.object(runner, "selected_model_feature_importance", return_value=[{"name": "priority", "importance": .2}]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_ready_endpoints_are_tested_before_private_config_merge(self):
        path = self.settings["app_config_path"]
        path.write_text(json.dumps({"api_token": "rag-token", "endpoints": {"responses": "https://rag"},
                                    "endpoint_tokens": {"responses": "rag-token"},
                                    "endpoint_ca_bundles": {"responses": "rag-ca", "tabular_scoring": "stale-ca"},
                                    "endpoint_tls_strict": {"responses": False}}))
        with patch.object(runner, "wait_for_inference_service", return_value=self.cluster.get_namespaced_custom_object.return_value), \
                patch.object(runner, "verify_automl_endpoint") as verify:
            runner.deploy_automl_runs(self.settings, ["tabular", "timeseries"], wait=True, dry_run=False)
        self.assertEqual(verify.call_count, 2)
        result = json.loads(path.read_text())
        self.assertEqual(result["endpoint_tokens"]["responses"], "rag-token")
        self.assertEqual(result["api_token"], "rag-token")
        self.assertEqual(result["endpoint_ca_bundles"]["responses"], "rag-ca")
        scoring_ca = Path(result["endpoint_ca_bundles"]["tabular_scoring"])
        self.assertTrue(scoring_ca.is_file())
        self.assertEqual(result["endpoint_ca_bundles"]["timeseries_scoring"], str(scoring_ca))
        ssl.create_default_context(cafile=str(scoring_ca))
        self.assertEqual(result["endpoint_tls_strict"], {"responses": False})
        self.assertEqual(result["endpoint_tokens"]["tabular_scoring"], "cluster-token")
        self.assertEqual(result["tabular_feature_importance"]["features"][0]["name"], "priority")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_prediction_failure_preserves_existing_config(self):
        path = self.settings["app_config_path"]
        path.write_text('{"api_token":"existing","endpoints":{}}')
        before = path.read_bytes()
        with patch.object(runner, "verify_automl_endpoint", side_effect=RuntimeError("invalid prediction")):
            with self.assertRaisesRegex(RuntimeError, "invalid prediction"):
                runner.deploy_automl_runs(self.settings, ["tabular"], wait=False, dry_run=False)
        self.assertEqual(path.read_bytes(), before)

    def test_probability_output_is_enabled_only_on_tabular_service(self):
        tabular = runner.inference_service_manifest(self.settings, "tabular", "model", "runtime", "tabular")
        forecast = runner.inference_service_manifest(self.settings, "forecast", "model", "runtime", "timeseries")
        self.assertEqual(tabular["spec"]["predictor"]["model"]["env"], [{"name": "PREDICT_PROBA", "value": "true"}])
        self.assertNotIn("env", forecast["spec"]["predictor"]["model"])

    def test_existing_service_enables_probabilities_and_preserves_other_environment(self):
        original = [{"name": "OTHER", "valueFrom": {"secretKeyRef": {"name": "connection", "key": "value"}}},
                    {"name": "PREDICT_PROBA", "value": "false"}]
        self.cluster.get_namespaced_custom_object.return_value = {"spec": {"predictor": {"model": {"env": original}}}}
        runner.enable_tabular_probabilities(self.cluster, self.settings, "tabular")
        updated = self.cluster.patch_namespaced_custom_object.call_args.kwargs["body"]["spec"]["predictor"]["model"]["env"]
        self.assertEqual(updated[0], original[0])
        self.assertEqual(updated[1], {"name": "PREDICT_PROBA", "value": "true"})
        self.assertEqual(original[1]["value"], "false")
        self.cluster.reset_mock()
        self.cluster.get_namespaced_custom_object.return_value = {"spec": {"predictor": {"model": {"env": updated}}}}
        runner.enable_tabular_probabilities(self.cluster, self.settings, "tabular")
        self.cluster.patch_namespaced_custom_object.assert_not_called()

    def test_redeploying_existing_tabular_service_reconciles_probability_mode(self):
        self.cluster.create_namespaced_custom_object.side_effect = ApiException(409)
        with patch.object(runner, "enable_tabular_probabilities") as enable, patch.object(runner, "verify_automl_endpoint"):
            runner.deploy_automl_runs(self.settings, ["tabular", "timeseries"], wait=False, dry_run=False)
        enable.assert_called_once_with(self.cluster, self.settings, "city-services-tabular-tabular")

    def test_failed_run_is_rejected_before_cluster_creation(self):
        self.pipeline.get_run.side_effect = [types.SimpleNamespace(state="SUCCEEDED"), types.SimpleNamespace(state="FAILED")]
        with self.assertRaisesRegex(RuntimeError, "requires a successful run"):
            runner.deploy_automl_runs(self.settings, ["tabular", "timeseries"], wait=True, dry_run=False)
        self.cluster.create_namespaced_custom_object.assert_not_called()

    def test_dry_run_does_not_create_resources_test_endpoints_or_save_config(self):
        with patch.object(runner, "verify_automl_endpoint") as verify, patch.object(runner, "update_app_config") as save:
            runner.deploy_automl_runs(self.settings, ["tabular"], wait=True, dry_run=True)
        self.cluster.create_namespaced_custom_object.assert_not_called()
        verify.assert_not_called()
        save.assert_not_called()

    def test_deployment_smoke_checks_use_real_app_payload_and_adapters(self):
        for kind, payload in (("tabular", {"predictions": [[1]]}), ("timeseries", {"predictions": list(range(7))})):
            with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as upstream:
                runner.verify_automl_endpoint(self.settings, kind + "_scoring", "https://model.example")
            request = upstream.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer cluster-token")
            instances = json.loads(request.data)["instances"]
            if kind == "tabular":
                self.assertEqual(instances[0]["priority"], ["Standard"])
                self.assertEqual(instances[0]["sla_days"], [4])
            else:
                self.assertEqual(len(instances), 35)
                self.assertIsInstance(instances[0]["target"], float)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"predictions": []}')):
            with self.assertRaisesRegex(RuntimeError, "scoring check failed"):
                runner.verify_automl_endpoint(self.settings, "timeseries_scoring", "https://model.example")

    def test_scoring_ca_keeps_explicit_trust_settings_and_rejects_empty_store(self):
        for values in ({"app_endpoint_ca_bundle": "explicit.pem"}, {"app_endpoint_verify_ssl": False}):
            settings = dict(self.settings, **values)
            self.assertIs(runner.automl_endpoint_settings(settings), settings)
        with patch("ssl.create_default_context") as context:
            context.return_value.get_ca_certs.return_value = []
            with self.assertRaisesRegex(RuntimeError, "No trusted CA"):
                runner.automl_endpoint_settings(self.settings)

    def test_unattended_all_deploys_both_families_without_prompts(self):
        args = types.SimpleNamespace(non_interactive=True, wait=False, auto_deploy_automl=True,
                                     auto_deploy_autorag=True, poll_interval=15)
        submitted = {"tabular": "t", "timeseries": "s", "autorag": "r"}
        with patch.object(runner, "monitor_runs", return_value={key: "SUCCEEDED" for key in submitted.values()}), \
                patch.object(runner, "deploy_automl_runs") as automl, patch.object(runner, "deploy_autorag_run") as rag, \
                patch.object(runner, "confirm") as confirm:
            runner.post_submit_guide(self.pipeline, self.settings, submitted, args)
        automl.assert_called_once_with(self.settings, ["t", "s"], wait=True, dry_run=False)
        rag.assert_called_once()
        confirm.assert_not_called()

    def test_one_family_failure_still_attempts_other_and_returns_failure(self):
        args = types.SimpleNamespace(non_interactive=True, wait=False, auto_deploy_automl=True,
                                     auto_deploy_autorag=True, poll_interval=15)
        with patch.object(runner, "monitor_runs", return_value={"t": "SUCCEEDED", "r": "SUCCEEDED"}), \
                patch.object(runner, "deploy_automl_runs", side_effect=RuntimeError("scoring failed")), \
                patch.object(runner, "deploy_autorag_run") as rag:
            with self.assertRaisesRegex(RuntimeError, "Automatic deployment incomplete: scoring failed"):
                runner.post_submit_guide(self.pipeline, self.settings, {"tabular": "t", "autorag": "r"}, args)
        rag.assert_called_once()

    def test_setup_exposes_existing_model_deployment_and_waits_automatically(self):
        env = self.root / ".env"
        env.touch()
        with patch.object(sys, "argv", ["setup", "--env-file", str(env), "--deploy", "t", "s", "--apply"]), \
                patch.object(setup, "run", return_value=0) as run:
            with self.assertRaises(SystemExit) as stopped:
                setup.main()
        self.assertEqual(stopped.exception.code, 0)
        for call in run.call_args_list:
            command = call.args[0]
            index = command.index("--deploy")
            self.assertEqual(command[index + 1:index + 3], ["t", "s"])
            self.assertIn("--wait-deploy", command)

    def test_auto_deploy_cli_maps_to_the_selected_pipelines(self):
        for selected, automl, rag in (("all", True, True), ("tabular", True, False),
                                      ("timeseries", True, False), ("autorag", False, True)):
            with patch.object(sys, "argv", ["runner", "--auto-deploy", "--only", selected]), \
                    patch.object(runner, "load_env"), patch.object(runner, "config", return_value={"prefix": "demo"}), \
                    patch.object(runner, "validate_inputs"), patch.object(runner, "upload_data"), \
                    patch.object(runner, "submit", return_value={}), patch.object(runner, "post_submit_guide") as guide:
                runner.main()
            args = guide.call_args.args[-1]
            self.assertEqual(args.auto_deploy_automl, automl)
            self.assertEqual(args.auto_deploy_autorag, rag)

    def test_existing_automl_and_autorag_runs_can_be_deployed_together(self):
        with patch.object(sys, "argv", ["runner", "--deploy", "tabular", "timeseries", "--deploy-autorag", "rag", "--wait-deploy"]), \
                patch.object(runner, "load_env"), patch.object(runner, "config", return_value=self.settings), \
                patch.object(runner, "deploy_automl_runs") as automl, patch.object(runner, "deploy_autorag_run") as rag, \
                patch.object(runner, "upload_data") as upload:
            runner.main()
        automl.assert_called_once_with(self.settings, ["tabular", "timeseries"], True, False)
        rag.assert_called_once_with(self.settings, "rag", dry_run=False)
        upload.assert_not_called()

    def test_setup_forwards_one_command_full_deployment_to_preview_and_execution(self):
        env = self.root / ".env"
        env.touch()
        with patch.object(sys, "argv", ["setup", "--env-file", str(env), "--auto-deploy", "--apply"]), \
                patch.object(setup, "run", return_value=0) as run:
            with self.assertRaises(SystemExit) as stopped:
                setup.main()
        self.assertEqual(stopped.exception.code, 0)
        for call in run.call_args_list:
            self.assertIn("--auto-deploy", call.args[0])
            self.assertIn("--non-interactive", call.args[0])


if __name__ == "__main__":
    unittest.main()
