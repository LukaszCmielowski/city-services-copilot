# Setup reference

Use the [README](../README.md) for the normal demo flow. This reference covers manual setup, deployment options, and troubleshooting.

- [Platform prerequisites](#1-prerequisites)
- [Data and manual pipeline setup](#bundled-data-layout)
- [Runner and deployment options](#automated-cluster-run)
- [Manual app configuration](#5-connect-and-launch-the-app)
- [Credential refresh](#refresh-credentials)
- [Troubleshooting](#troubleshooting)

## 1. Prerequisites

### Local machine

You need Python 3.9 or later and an available local port 8000. No Python packages are required for the sample UI.

### OpenShift AI project

Ask the platform administrator to confirm each item before running the pipelines:

- You have **Editor** access to the project.
- An existing pipeline server can run the managed AutoML and AutoRAG pipelines, or matching definitions have been imported. The runner does not create a DSPA. If using the dashboard, the AutoML and Gen AI/AutoRAG views are enabled.
- Compute quota supports the `speed` preset (4 vCPU, 16 GiB) and, if used, `balanced` (8 vCPU, 32 GiB).
- One existing S3 connection, named in `.env` as `S3_CONNECTION_NAME`, is available to AutoML, AutoRAG evaluation/documents, and KServe deployment. It contains `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_S3_ENDPOINT`; region is optional. Use the documented advanced overrides only when those stages genuinely use different connections.
- A MaaS secret exists with `MAAS_BASE_URL` and `MAAS_API_KEY`.
- A remote Milvus secret (`MILVUS_*`, minimum `MILVUS_URI`) or PGVector secret (`PGVECTOR_*`) exists. Inline vector databases are unsupported.
- You know at least one embedding-model ID and one generation-model ID exposed by MaaS. The AutoRAG pipeline requires explicit lists.

Record the project, secret *names*, bucket names, S3 prefix, MaaS secret, and database secret in a private handoff. Keep `.env` private; never commit secret values or send them to the browser.

**Continue only when:** required connections and secret names are available in the project.

## Bundled data layout

All inputs required for the demo are already in the repository:

```text
data/
├── prepared/
│   ├── resolution-risk.csv
│   └── service-demand-daily.csv
├── sample-requests.json          # five fixed reports shown in the app
├── guidance/                    # original demo summaries with official links
└── autorag-evaluation.json
```

Synthetic fixtures for `prepared/` and twenty-two guidance documents are already included, so validation and upload work immediately. The CSVs meet the 1,000-record AutoML minimum. The bundled guidance files are concise demo summaries with official-source links.

### Validate bundled data

```bash
python3 scripts/validate_demo_inputs.py
```

It verifies schema, UTF-8, non-empty files, evaluation JSON, and full S3 document keys.

### Upload to S3-compatible storage

The cluster runner uploads all inputs automatically. For a manual upload, use the included uploader after configuring the AWS CLI with credentials that can write to the target bucket. It uploads the bundled prepared AutoML data, guidance documents, and evaluation JSON. It runs in dry-run mode by default:

```bash
scripts/upload_demo_data.sh \
  --bucket YOUR_BUCKET \
  --profile YOUR_AWS_PROFILE \
  --endpoint-url https://YOUR-S3-ENDPOINT
```

Review the listed destinations, then add `--apply` to perform the upload. Omit `--profile` for the AWS CLI default profile, and omit `--endpoint-url` when using AWS S3.

The uploader uses these exact object keys:

```text
city-services-copilot/automl/resolution-risk.csv
city-services-copilot/automl/service-demand-daily.csv
city-services-copilot/autorag/guidance/<guidance-files>
city-services-copilot/autorag/autorag-evaluation.json
```

**Continue only when:** the validator succeeds and all files are visible through the project connection.

## 3. Train AutoML models

Both pipelines ingest source data from S3, write internal split data on the pipeline workspace, and publish an HTML leaderboard plus refitted `_FULL` model artifacts.

### SLA-risk classification

Start `autogluon-tabular-training-pipeline` from the pipeline dashboard or KFP client.

| Parameter | Value |
| --- | --- |
| `train_data_secret_name` | AutoML S3 secret. |
| `train_data_bucket_name` | Prepared-data bucket. |
| `train_data_file_key` | `city-services-copilot/automl/resolution-risk.csv` |
| `label_column` | `sla_miss` |
| `task_type` | `binary` |
| `positive_class` | `1` for a 0/1 label, otherwise exact positive label. |
| `eval_metric` | Data-owner-approved metric. Empty defaults to `accuracy`. |
| `top_n` | `3` |
| `preset` | `speed` first; `balanced` after a successful baseline. |

Leave `test_data_bucket_name` and `test_data_file_key` empty for the pipeline's stratified holdout, or supply both for a fixed labeled test set.

Inspect the leaderboard, metrics, feature importance, confusion matrix, ROC/PR curves, and generated predictor notebook. Select a model only after checking recall at the operational alert threshold and errors by service type and neighborhood. Record its artifact:

```text
models_artifact/<ModelName>_FULL/predictor/
```

### Demand forecast

Start `autogluon-timeseries-training-pipeline` with the same S3 secret and bucket.

| Parameter | Value |
| --- | --- |
| `train_data_file_key` | `city-services-copilot/automl/service-demand-daily.csv` |
| `target` | `target` |
| `timestamp_column` | `timestamp` |
| `id_column` | `item_id` |
| `prediction_length` | `7` |
| `top_n` | `3` |
| `preset` | `speed` |

Leave `known_covariates_names` empty unless their values are known for every future day. Leave external test fields empty for the per-series temporal holdout. The default metric is `mean_absolute_scaled_error`. Inspect performance by service type and peak day, then record the selected `_FULL` artifact and notebook.

**Continue only when:** selected tabular and time-series `_FULL` model paths and performance evidence are recorded.

## 4. Run AutoRAG

AutoRAG supports PDF, DOCX, PPTX, Markdown, HTML, TXT, ODT, ODP, AsciiDoc, LaTeX, EPUB, and EML inputs.

Start `documents-rag-optimization-pipeline` from the pipeline dashboard or KFP client.

| Parameter | Value |
| --- | --- |
| `test_data_secret_name`, `test_data_bucket_name`, `test_data_key` | Evaluation S3 secret, bucket, and `city-services-copilot/autorag/autorag-evaluation.json`. |
| `input_data_secret_name`, `input_data_bucket_name` | Guidance-corpus S3 secret and bucket. |
| `input_data_keys` | `['city-services-copilot/autorag/guidance/']` |
| `maas_secret_name` | MaaS URL/API-key secret. |
| `db_secret_name` | Remote Milvus or PGVector secret. |
| `embedding_models` | Explicit list of embedding model IDs. |
| `generation_models` | Explicit list of generation model IDs. |
| `optimization_metric` | `unitxt:faithfulness` for first run. |
| `optimization_max_rag_patterns` | `5` (allowed range: `4`–`10`). |
| `preset` | `speed` for first run. |

`speed` supports Unitxt and custom metrics. Use `balanced` for table parsing, hybrid chunking, and LLM contextual enrichment; it additionally supports RAGAS metrics such as `ragas:context_precision`. `custom:overall_score` is also valid.

Inspect the HTML leaderboard, retrieval/citation quality, and answers to representative questions. Retain the selected pattern's `pattern.json`, evaluation results, and generated notebooks.

Use the selected inference notebook to inspect a pattern in a workbench with the same S3, MaaS, and database connections. Confirm that its vector-store collection is indexed before invocation. Use the indexing pipeline/notebook when indexing is needed; re-index after a corpus change. The starter kit uses the existing collection.

**Continue only when:** the selected pattern answers evaluation questions with attributable corpus documents and its artifact location is recorded.

### Invoke the selected AutoRAG pattern

Deploy the generated starter kit after optimization succeeds. The app uses its native `/v1/responses` API with the kit's API-key authentication. The [automated deployment](#deploy-and-connect-the-best-autorag-starter-kit) prepares the Red Hat downstream runtime.

#### Download and deploy the starter kit

The [automated starter-kit deployment](#deploy-and-connect-the-best-autorag-starter-kit) prepares the supported runtime and connects the app. The instructions below describe manual inspection and deployment. An older downloaded archive may contain different deployment instructions; use the runner to prepare this repository's supported runtime rather than combining commands from different versions.

1. Open the successful AutoRAG run's results and download `starter_kit.zip`. If using object storage, find that exact key under the run's optimization artifacts; do not infer it from a model-scoring URL. Also retain the selected `pattern.json` and confirm the kit's `agent_config.json` names the intended model and collection.
2. Extract the archive into a private working directory and read its README. The current downstream kit requires `oc`, running `podman`, `uv`, `make`, Python, `curl`, the Agent Sandbox operator, and a preinstalled Red Hat cluster gateway. It runs the RHOAI CLI in a container. Use the automated deployment command to prepare the downstream deployment files from an existing successful run. Its generation model must support tool calling.
3. Confirm `oc` targets the intended cluster and build project. Review `values.yaml` for the pattern and existing MaaS/database secret names, and `agent_config.json` for models and the indexed collection. For the runner-prepared kit, run these commands from its `starter_kit` directory:

```bash
uv lock --upgrade-package ai4rag
make refresh-certs
make build-openshell
make deploy-openshell
```

An administrator installs the gateway separately; `make refresh-certs` registers it locally with its mTLS certificates. The deployment creates cluster resources and exposes an agent Route. Do not run `make load-docs` against the existing collection; use the separate indexing workflow when needed.

4. Record the resulting Route and API key using the kit's deployment output/instructions. Check the deployed `/docs` contract, then invoke it with a city-services question. For the gateway-generated CA, pass `--cacert /path/to/gateway-ca.crt` to curl; the prepared kit saves it under `~/.config/openshell/gateways/openshift/mtls/ca.crt` by default.

```bash
export AUTORAG_AGENT_URL='https://YOUR-AGENT-ROUTE'
# Set AUTORAG_AGENT_TOKEN privately to the deployed agent's API key.
curl --fail-with-body --silent --show-error \
  --cacert "$HOME/.config/openshell/gateways/openshift/mtls/ca.crt" \
  "${AUTORAG_AGENT_URL%/}/v1/responses" \
  -H "X-Api-Key: ${AUTORAG_AGENT_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"input":"How should an operator route an illegal-dumping report?","stream":false}'
```

5. In the existing `app-config.json`, set `endpoints.responses` to that `/v1/responses` URL, `endpoint_tokens.responses` to the agent API key, and `responses_auth_header` to `"X-Api-Key"`. For a gateway-generated CA, preserve the runner's `endpoint_ca_bundles.responses` and `endpoint_tls_strict.responses` settings, or use automated deployment to configure and verify them. Preserve the existing AutoML endpoint configuration. Restart `python3 main.py` and ask the same question in the guidance panel.

The agent can return answers without structured citation metadata. The app labels returned URLs matching the bundled corpus as **Agent references**, preserving structured citations separately when provided. An answer without accepted references says **No references returned · verify before use**. Corpus matching validates the URL, not whether the document was retrieved or supports every claim; review the linked guidance before use.

**Continue only when:** invocation succeeds directly and through `/api/ask`, and the answer's evidence comes from the intended collection.

## Runner and deployment options

### Automated cluster run

For direct control, use the cluster runner with the `.env` and dependencies created in the [README](../README.md#run-the-demo). These commands preview and submit pipelines; deployment is optional:

```bash
python3 scripts/run_cluster_demo.py --dry-run
python3 scripts/run_cluster_demo.py
```

The dry run validates the bundled data and configuration, then shows a compact submission plan—pipeline, S3 inputs, existing connection names, targets, and selected models—without writing to the cluster. Add `--show-parameters` only when troubleshooting and you need the full KFP parameter JSON. The normal command uploads the bundled synthetic CSVs, documents, and evaluation JSON; then submits the tabular AutoML, time-series AutoML, and AutoRAG managed pipelines. After submission in an interactive terminal, it asks whether to watch all runs. Successful runs offer deployment of the best AutoRAG starter kit and selected AutoML models; deployment prompts default to **No**. Use `--wait` to start watching without the first prompt, `--non-interactive` for unattended submission, `--only tabular`, `--only timeseries`, or `--only autorag` to run one pipeline, and `--skip-upload` only after a successful upload. Add `--auto-deploy` to wait and deploy all selected results without further prompts. Automated deployment attempts both AutoML and AutoRAG even if one deployment fails, and exits unsuccessfully if any requested step remains incomplete.

### Optional advanced configuration

The short `.env.example` is the supported default. Add an override only when the default does not match your platform: `RHOAI_KFP_URL` for a direct pipeline URL; `AUTOML_S3_SECRET_NAME`, `AUTORAG_TEST_S3_SECRET_NAME`, `AUTORAG_DOCUMENTS_S3_SECRET_NAME`, or `AUTOML_KSERVE_STORAGE_KEY` when those stages use different S3 connections; `PIPELINE_ARTIFACTS_S3_BUCKET` and `PIPELINE_ARTIFACTS_S3_PREFIX` when artifacts are stored separately; or the TLS, runtime, pipeline-name, and preset variables documented inline in `scripts/run_cluster_demo.py`. Existing `.env` files using the former specific S3 variables remain supported.

### Monitor submitted runs and leaderboards

Use the submitted run IDs to check status without resubmitting anything:

```bash
python3 scripts/run_cluster_demo.py --status TABULAR_RUN_ID TIMESERIES_RUN_ID AUTORAG_RUN_ID --watch
```

The monitor shows run links, status, ranked leaderboards, and artifact locations. By default, artifact lookup uses `AWS_S3_BUCKET`; set `PIPELINE_ARTIFACTS_S3_BUCKET` and, if applicable, `PIPELINE_ARTIFACTS_S3_PREFIX` when artifacts are stored elsewhere. Retrieval uses the S3 credentials in `.env`.

### Deploy the selected AutoML models

After the tabular and time-series runs have succeeded, the runner can select each run's highest-scoring predictor artifact and create a persistent KServe `InferenceService` for it:

```bash
python3 scripts/run_cluster_demo.py --deploy TABULAR_RUN_ID TIMESERIES_RUN_ID --wait-deploy
# Equivalent guided entry point, with a dry-run preflight and automatic readiness wait:
python3 scripts/setup_demo.py --deploy TABULAR_RUN_ID TIMESERIES_RUN_ID --apply
```

Add `--dry-run` to inspect the chosen model and generated `InferenceService` manifest without creating anything; it reads KFP run status and model artifacts from object storage. Each run must have succeeded before serving resources are created.

This is an explicit cluster write: for each endpoint, it clones the OpenShift AI `autogluon-runtime-template` into a namespace-scoped `ServingRuntime` with the same name, waits for KServe to index it, then creates the matching `InferenceService` in `RHOAI_PROJECT_NAME`. It preserves the template's dashboard metadata, so the OpenShift AI dashboard identifies it as **AutoGluon ServingRuntime for KServe** rather than an unknown runtime; re-running `--deploy` repairs that metadata on an existing demo runtime. The command reuses the one existing object-storage connection named by `S3_CONNECTION_NAME` for AutoML, AutoRAG, and KServe deployment; its default service account is `<S3_CONNECTION_NAME>-sa`. That connection must have read access to the pipeline-artifact bucket. To reuse a platform-provided runtime instead, set `AUTOML_SERVING_RUNTIME_NAME` and set `AUTOML_CREATE_SERVING_RUNTIME=false`. The AutoML deployment path does not create or alter secrets, service accounts, or Model Registry records. The tabular InferenceService sets `PREDICT_PROBA=true`, supported by the [AutoGluon runtime](https://kserve.github.io/website/docs/model-serving/predictive-inference/frameworks/autogluon); existing tabular services are reconciled on redeployment. The shared ServingRuntime and time-series service are unchanged.

After the services are ready, the runner checks authenticated scoring and saves their URLs and `RHOAI_TOKEN` credentials to private `app-config.json` (or `DEMO_APP_CONFIG_PATH`). It also imports the tabular model's global feature importance for **Model details**. Existing AutoRAG settings are preserved; a failed scoring check leaves the app configuration unchanged.

### Deploy and connect the best AutoRAG starter kit

For an already successful AutoRAG run, use:

```bash
python3 scripts/setup_demo.py --deploy-autorag AUTORAG_RUN_ID --dry-run
python3 scripts/setup_demo.py --deploy-autorag AUTORAG_RUN_ID --apply
```

The first command reads the run and its artifacts, downloads the archive into a temporary directory, and validates the generated kit configuration. It does not build, deploy, or change the app configuration. The second command performs the deployment. The runner also accepts `python3 scripts/run_cluster_demo.py --deploy-autorag AUTORAG_RUN_ID` directly.

For submission through deployment without further prompts:

```bash
python3 scripts/setup_demo.py --only autorag --auto-deploy-autorag --apply
```

Current starter kits require a ready Red Hat OpenShell gateway in the cluster's `openshell` namespace and the Red Hat build of the Agent Sandbox operator. Install `oc`, `podman`, `uv`, `make`, `curl`, and Python locally. Podman must be running; on macOS, start its machine if needed. The kit runs the RHOAI CLI in a container, so a local OpenShell CLI installation is no longer required. `make refresh-certs` retrieves mTLS certificates and registers the cluster gateway as `openshift`. Gateway installation remains a separate administrator prerequisite. The OpenShift internal image registry must be available, the generation model must support tool calling, and the pattern's collection must already be indexed.

The runner prepares the bundled Red Hat downstream deployment files while preserving the selected run's optimized application, configuration, and indexed collection.

The runner:

1. Downloads `starter_kit.zip` from the successful run's artifacts. A run without a generated kit must be rerun with a pipeline that produces one.
2. Uses the kit's existing MaaS/database connections and indexed collection, without uploading or re-indexing documents.
3. Refreshes gateway certificates and registration, updates the dependency lockfile, and builds the agent image inside OpenShift using the platform's Red Hat images. Podman runs the CLI locally.
4. Deploys the sandbox, configures database/model access, exposes the agent Route, and creates its authentication tokens.
5. Checks the agent's health and authenticated Responses API, then saves its endpoint, token, and TLS settings to private `app-config.json`, preserving AutoML configuration. Restart `python3 main.py` to load it.

Deployment files and a private log remain under Git-ignored `run-results/autorag/`. The temporary kubeconfig is outside the image build context, targets `RHOAI_URL` and `RHOAI_PROJECT_NAME`, and is removed after deployment. App credentials and logs use owner-only file permissions. A failed build or endpoint check leaves the app configuration unchanged; cluster resources already created remain available for inspection. Re-running deployment rebuilds the image and replaces the demo sandbox. The kit shares the Route name, `agent-client` service account, and `agent-client-token` secret, so run one demo deployment at a time. It requests 168-hour client/reviewer tokens; cluster policy may shorten their lifetime. Re-deploy before token expiry.

#### Deployment namespaces and Agent Ops

`RHOAI_PROJECT_NAME` selects the pipeline, image-build, connection-secret, and agent-client-token project. The gateway manages the Sandbox in its own namespace, `openshell`; with the default sandbox name, its Kubernetes resource is `default--city-services-rag`. Changing the current `oc` project does not move it. In Agent Ops, select **All projects** or **openshell** to see this deployment.

The demo runner requires the existing gateway in `openshell`; there is no empty namespace field to fill. The standalone prepared Makefile accepts `OPENSHELL_NAMESPACE` for another existing gateway, but the demo runner's complete flow supports `openshell` only.

The agent uses LangChain. The starter-kit deployment currently omits the top-level Sandbox annotation `opendatahub.io/agent-framework: langchain`, so Agent Ops may leave **Framework** blank. The generator fix is tracked in [RHAI-19949](https://redhat.atlassian.net/browse/RHAI-19949). To set it on an existing default demo deployment, using `oc` authenticated to the intended cluster:

```bash
oc annotate sandbox default--city-services-rag -n openshell opendatahub.io/agent-framework=langchain --overwrite
```

Substitute the actual Sandbox resource name if customized. This annotation does not require an image rebuild; it must be reapplied if deployment replaces the Sandbox until the generator fix is available.

Optional overrides:

| Variable | Default / purpose |
| --- | --- |
| `RHOAI_CSV_NAMESPACE` | `redhat-ods-operator`; namespace used to discover supported downstream images. |
| `AUTORAG_BUILD_PULL_SECRET_NAME` | Existing project Docker registry secret for build-time pulls. By default, use the builder service account's first secret with Red Hat or Quay credentials. The runner passes its name explicitly to the Docker BuildConfig. |
| `OPENSHELL_BASE_IMAGE`, `OPENSHELL_CLI_IMAGE` | Explicit downstream image overrides; otherwise selected from the RHOAI CSV. |
| `AUTORAG_STARTER_KIT_S3_KEY` | Exact key to disambiguate multiple kit archives within the run's artifact prefix. |
| `AUTORAG_DEPLOY_MAAS_SECRET_NAME`, `AUTORAG_DEPLOY_DB_SECRET_NAME` | Explicit overrides for the connection names in the kit's `values.yaml`. |
| `AUTORAG_SANDBOX_NAME` | `city-services-rag`; deployment replaces this sandbox. |
| `AUTORAG_DEPLOY_TIMEOUT_SECONDS` | `1800` per local build/deployment command. |
| `AUTORAG_HEALTH_TIMEOUT_SECONDS` | `300` for health readiness and the authenticated answer request. |
| `AUTORAG_SMOKE_QUESTION` | City-services illegal-dumping routing question. |
| `RHOAI_OPENSHIFT_API_CA_BUNDLE` | Optional absolute cluster API CA PEM path for the deployment's isolated kubeconfig. |
| `APP_ENDPOINT_CA_BUNDLE`, `APP_ENDPOINT_VERIFY_SSL` | Route TLS settings used by both verification and the app. |

Gateway access uses TLS and mTLS client certificates. Keep `OPENSHELL_GATEWAY_INSECURE` disabled and preserve the endpoint TLS settings written by deployment. Agent answers may have no structured citations; inspect evidence as described in [the manual starter-kit instructions](#download-and-deploy-the-starter-kit).

## 5. Connect and launch the app

### Enable live mode

Deploy the selected tabular and time-series AutoML models as scoring endpoints. The backend scores the queue through the tabular endpoint and requests the matching forecast when a report is selected. Configure AutoRAG using [the starter kit above](#invoke-the-selected-autorag-pattern).

Automated deployment writes the private app configuration for you. For first-time manual configuration only, copy the template and fill in endpoint URLs and separate credentials. Preserve an existing configuration rather than overwriting it:

```bash
cp app-config.example.json app-config.json
```

`app-config.json` is ignored by Git and loaded once, at backend startup. The app does not load `.env`. Start it with `python3 main.py`; each configured endpoint enables live inference for its matching feature, while unconfigured features use sample mode. Omit the file for an entirely local sample.

| Configuration | Credential / purpose |
| --- | --- |
| `endpoint_tokens.tabular_scoring`, `endpoint_tokens.timeseries_scoring` | OpenShift bearer tokens with access to the KServe endpoints. The runner copies `RHOAI_TOKEN` after successful endpoint tests. |
| `endpoint_tokens.responses` | Separate agent-client service-account token, sent with `responses_auth_header: "X-Api-Key"`. This is not the MaaS API key. |
| `api_token` | Required string; fallback bearer token when a per-endpoint token is absent. |
| `endpoint_ca_bundles`, `endpoint_tls_strict` | Endpoint-specific TLS settings written and tested by deployment. Keep referenced CA files. |

Both AutoML and AutoRAG deployment update their own entries and preserve the others. `DEMO_APP_CONFIG_PATH` changes only the runner's output location; the app still reads repository-root `app-config.json`. Keep the default for normal use. The local server allowlists only UI assets and the rendered setup guide; it never serves `.env`, `app-config.json`, data files, or other repository files over HTTP.

### Refresh credentials

For expired scoring credentials, update `RHOAI_TOKEN` in `.env` with a fresh token for the intended cluster and project. Exported environment variables take precedence over `.env`; update or unset an old exported `RHOAI_TOKEN` before running setup. Redeploy the existing successful AutoML runs with `--deploy ... --apply` to test both endpoints and refresh their app credentials without retraining. Alternatively, update both scoring entries in `endpoint_tokens` directly, preserving the guidance token and TLS settings.

For an expired guidance token, redeploy the existing successful AutoRAG run with `--deploy-autorag ... --apply`. This refreshes the kit's client/reviewer tokens and verifies the agent; it rebuilds and replaces the sandbox but does not rerun optimization or upload data. The kit requests a seven-day token lifetime, which cluster policy may shorten.

After either change, stop and restart `python3 main.py`, then rescore the queue or retry guidance. Updating `.env` alone does not refresh credentials in a running app. A changed MaaS credential belongs in the cluster connection secret used by the agent, not in the app's `responses` token.

## 6. Validate and hand off

Before presentation, verify:

- Classification uses only published features and returns SLA-miss probabilities with `PREDICT_PROBA=true` on the tabular service. Confirm `risk` is the probability of class `1`; `sla_days` is a service target, not predicted resolution time. Probability calibration has not been assessed for this demo.
- Forecast returns exactly seven daily points for the selected service/neighborhood, labeled Day 1–Day 7.
- Review policy answers against the configured corpus. Source status must distinguish returned citations, generated references, and answers without sources; clickable citations require URL metadata from the agent.
- Out-of-corpus questions return a clear limitation, not fabricated policy.
- Selecting different reports updates their read-only facts, scores, matching demand, and guidance context. Switching reports clears the previous draft.

If the cluster is unavailable, use sample mode and clearly state that its results are illustrative.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| AutoML cannot see a CSV | S3 secret, bucket/key, UTF-8/comma CSV, header row, permissions. |
| Pipeline Route discovery times out | Verify `RHOAI_URL`, token permissions, and VPN/network access. Discovery times out after `RHOAI_OPENSHIFT_API_TIMEOUT_SECONDS` (default 20). Set `RHOAI_KFP_URL` to use a direct pipeline URL. |
| Classification metric is misleading | Class balance, temporal validation, precision/recall at alert threshold. |
| AutoRAG cannot find document keys | `correct_answer_document_keys` exactly match uploaded S3 object keys. |
| AutoRAG cannot start | Test-data S3, document S3, MaaS, Milvus/PGVector, embedding list, generation list. |
| Queue scoring fails with HTTP 401/403 or no probability ranking | Refresh the scoring endpoint tokens, restart the backend, and rescore. If the response has only class labels, redeploy the tabular service to enable `PREDICT_PROBA=true`. See [Refresh credentials](#refresh-credentials). |
| AutoRAG returns HTTP 401/403 | Use the agent-client token in `endpoint_tokens.responses` with `X-Api-Key`. Refresh expired kit tokens through deployment; do not substitute the MaaS API key. |
| Answer has no references | Both prompts request source URLs, but the app links generated references only when they match the bundled corpus. Inspect the agent response and collection; do not treat an answer without accepted sources as verified guidance. |
| Agent Ops does not list the deployment or Framework is blank | Select All projects or openshell. The Sandbox is gateway-managed there; the missing LangChain annotation is tracked in RHAI-19949. See [Deployment namespaces and Agent Ops](#deployment-namespaces-and-agent-ops). |
| OpenShell reports no active gateway | Ensure the gateway is installed and ready in the `openshell` namespace. In the downstream kit, run `make refresh-certs` to fetch mTLS certificates and register `openshift` through the RHOAI CLI. The runner does this before checking authenticated status. |
| Sandbox stays `Provisioning` with `ConfigurationInvalid` | Inspect supervisor logs and confirm the sandbox base image selected from the RHOAI CSV matches the installed platform. The runner inherits the downstream base image policy. |
| Build log stream disconnects while the Build is still `Running` | The runner polls the Build status independently. Check the existing Build before retrying; a disconnected log stream does not mean the image build failed. |
| Build fails with `PullBuilderImageFailed` / registry `unauthorized` | Verify the Docker BuildConfig's `pullSecret`, including credentials for multi-stage base images. The runner selects an existing builder registry secret and passes it explicitly; use `AUTORAG_BUILD_PULL_SECRET_NAME` to select another existing project secret. |
| Sandbox is `Ready`, but Milvus startup fails with `Permission denied` and `transparent_tcp_policy_denied` | Confirm the prepared kit starts its local CONNECT proxy (`OPENSHELL_GRPC_PROXY_PORT=18080`). The runner always uses the pinned downstream deployment templates. Rebuild and redeploy the existing successful run; no data upload or AutoRAG pipeline rerun is required. |
| UI still shows sample results | Configure the matching URI in `app-config.json` and restart the backend. |

## Repository map

| Path | Purpose |
| --- | --- |
| `main.py` | Local web server, sample-mode behavior, and server-side endpoint client. |
| `index.html`, `styles.css`, `app.js` | Interactive demo UI. |
| `app-config.example.json` | Endpoint URL and separate scoring/guidance token template. |
| `service_catalog.py` | Shared service targets, synthetic risk factors, and guidance filenames. |
| `data/sample-requests.json` | The five fixed reports shown in the queue. |
| `data/README.md` | Bundled fixtures, request queue, guidance corpus, and upload conventions. |
| `data/autorag-evaluation.json` | Starter evaluation data. |
| `scripts/validate_demo_inputs.py` | Dependency-free data preflight validator. |
| `scripts/upload_demo_data.sh` | Validate and dry-run/upload prepared data to S3-compatible storage. |
| `scripts/setup_demo.py` | Interactive confirmation and preflight for the `.env` cluster runner. |
| `scripts/run_cluster_demo.py` | `.env`-driven upload, managed-pipeline submission, monitoring, deployment, and app configuration. |
| `scripts/deploy_autorag.py` | Generated starter-kit download, OpenShift build/deployment, endpoint verification, and private app configuration. |
| `scripts/autorag_runtime/` | Prepared downstream deployment files, instructions, and source attribution. |
| `scripts/generate_demo_fixtures.py` | Regenerate the deterministic synthetic AutoML fixture CSVs. |
| `.env.example`, `requirements-cluster.txt` | Cluster-runner configuration template and its Python dependencies. |
