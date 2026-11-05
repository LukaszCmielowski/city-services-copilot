# City Services Copilot

Help a 311 supervisor choose which requests to review first and prepare a clear resident response. AutoML estimates delay risk and matching demand; AutoRAG prepares handling steps using city service guidance.

The included reports and training data are synthetic. Model estimates demonstrate the integration, and generated guidance needs review before use.

## Run the demo

For live inference, you need:

- An OpenShift AI project with a pipeline server, S3 storage, MaaS models, and a vector database connection. The cluster must already have a Red Hat OpenShell gateway in `openshell` and the Agent Sandbox operator.
- Python, `oc`, running Podman, `uv`, `make`, and `curl` locally. See [platform prerequisites](docs/END_TO_END_GUIDE.md#1-prerequisites) for details.

1. Install the runner dependencies and create your private configuration:

   ```bash
   python3 -m pip install -r requirements-cluster.txt
   cp .env.example .env
   ```

   Fill in the cluster, project, storage credentials, connection names, and model IDs shown in `.env`.

2. Preview the plan, then run it:

   ```bash
   python3 scripts/setup_demo.py --auto-deploy --dry-run
   python3 scripts/setup_demo.py --auto-deploy --apply
   ```

   This uploads the bundled data, runs all three pipelines, deploys their results, and saves the endpoints and credentials to private `app-config.json`.

3. Start the app after deployment succeeds:

   ```bash
   python3 main.py
   ```

   Open [localhost:8000](http://localhost:8000). Restart an already running backend to load updated credentials.

For a local preview, run `python3 main.py` without `app-config.json`; no cluster or extra Python packages are needed. The app labels its results as samples.

## Present the workflow

1. **Score sample requests** to rank five reports by estimated SLA-miss probability. Delay risk is separate from safety urgency.
2. **Review** a report to see its cached risk and seven-day demand forecast. The forecast describes workload for the same service and neighborhood.
3. **Generate handling brief**, review its references, edit the resident-response draft, and **Copy response** into your service system. The app does not send it.

References are labeled separately from agent citations. If no references are returned, verify the guidance before using it.

![City Services Copilot: scored queue, selected request risk and forecast, and handling brief with references](docs/assets/sample-ui.png)

## Rerun without uploading data

Choose one action. Replace `*_RUN_ID` with actual successful run IDs.

| Goal | Command |
| --- | --- |
| Rerun all pipelines and deploy new results | `python3 scripts/setup_demo.py --skip-upload --auto-deploy --apply` |
| Rerun AutoRAG optimization and deploy its result | `python3 scripts/setup_demo.py --only autorag --skip-upload --auto-deploy-autorag --apply` |
| Redeploy an existing AutoRAG result | `python3 scripts/setup_demo.py --deploy-autorag AUTORAG_RUN_ID --apply` |
| Redeploy existing AutoML results | `python3 scripts/setup_demo.py --deploy TABULAR_RUN_ID TIMESERIES_RUN_ID --apply` |

`--skip-upload` requires input objects already present at the configured bucket and keys. Redeployment uses existing artifacts without retraining or re-indexing. Add `--dry-run` to preview a command, and restart the app after configuration changes. For expired tokens, see [Refresh credentials](#refresh-credentials).

## Refresh credentials

- **Scoring:** update `RHOAI_TOKEN` in `.env`, then redeploy the existing AutoML runs. An old exported `RHOAI_TOKEN` takes precedence over `.env`.
- **Guidance:** redeploy the existing AutoRAG run to refresh its separate agent-client token. This replaces the sandbox without rerunning optimization. The requested token lifetime is seven days; cluster policy may shorten it.

Restart the backend afterward. The app reads `app-config.json` at startup; changing `.env` alone does not refresh a running app. See [credential details](docs/END_TO_END_GUIDE.md#refresh-credentials) for manual updates.

## More details

[Setup reference](docs/END_TO_END_GUIDE.md) covers manual pipelines, advanced configuration, deployment namespaces, Agent Ops, and troubleshooting. [Data notes](data/README.md) describe the fixtures. Start with [troubleshooting](docs/END_TO_END_GUIDE.md#troubleshooting) if setup fails.
