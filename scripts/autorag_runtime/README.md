# Agentic RAG Starter Kit: `__PATTERN_NAME__`

This project was generated from an optimized RAG pattern. Before deploying the
application, make sure MaaS and the selected vector database are reachable.

These deployment instructions accompany the downstream runtime prepared by City
Services Copilot. The downloaded archive's original deployment files are retained
in the deployment workspace's `original-runtime/` directory, together with source
provenance. Application code and optimized configuration remain from the run.

The existing indexed collection is an input to this starter kit. Do not run
`make load-docs` against it. Re-indexing belongs to the separate document
indexing pipeline and could overwrite or corrupt the shared collection.

The application exposes `POST /v1/responses` and `GET /health`. The agent can
call `rewrite_query` to refine a search, call `retriever` to fetch documents,
and answer using the evidence found.

## OpenShell deployment

### Prerequisites

- An OpenShift cluster with `oc` logged in and permission to create the required
  resources.
- An OpenShell gateway installed on the cluster by an administrator (one-time
  cluster prerequisite, not part of this starter kit).
- `podman` installed and running locally (start its machine on macOS). The Makefile runs the OpenShell CLI from the RHOAI
  image in a container and stores the registration and the mTLS client
  certificates in `~/.config/openshell` (`OPENSHELL_CLI_STATE`), so no local
  OpenShell CLI installation is required.
- `uv` installed locally to update the dependency lockfile.
- `make`, `python3`, and `curl` installed locally for deployment, image discovery, and API CA checks.
- An OpenShift AI MaaS deployment exposing chat and embedding models, together
  with credentials. The chat model must support tool calling so the agent can
  invoke the retriever.
- A reachable Milvus, PostgreSQL/pgvector, or Neo4j instance and credentials,
  provided through the Kubernetes secrets referenced by `values.yaml`
  (`maas_secret_name` and `db_secret_name`).
  - For Milvus, provide `MILVUS_URI` and optionally `MILVUS_TOKEN` and
    `MILVUS_CA_CERT` for TLS.
  - For pgvector, provide `PGVECTOR_HOST` and optionally `PGVECTOR_PORT`,
    `PGVECTOR_DB`, `PGVECTOR_USER`, `PGVECTOR_PASSWORD`, and `PGVECTOR_CA_CERT`
    (PEM text for a private or self-signed CA).
  - For Neo4j, provide `NEO4J_URI` (for example `neo4j+s://host:7687`) and
    `NEO4J_PASSWORD`. `NEO4J_USERNAME` and `NEO4J_DATABASE` default to `neo4j`.
    For a private or self-signed CA, provide `NEO4J_CA_CERT` as PEM text.
- Neo4j patterns use graph search and require a collection already indexed in
  Neo4j. The starter kit serves retrieval from that collection; it does not
  index documents.
- The Red Hat build of the Agent Sandbox operator installed in the cluster.

`make build-openshell` selects the Red Hat OpenShell sandbox base from the latest
succeeded RHOAI ClusterServiceVersion (CSV), and builds its Python 3.12 runtime
using UBI. The containerized CLI also uses the RHOAI CSV image, with a matching
`quay.io/rhoai` fallback when needed. The CSV namespace defaults to
`redhat-ods-operator`; export `RHOAI_CSV_NAMESPACE` to select another namespace.
Export `OPENSHELL_BASE_IMAGE` or `OPENSHELL_CLI_IMAGE` to override image selection.
Editing only the Containerfile's `BASE_IMAGE` default does not override the
Makefile's build selection.

### Commands

Run the following commands from the starter-kit root directory, with `oc`
authenticated to the intended cluster and set to the build/connection project:

```bash
# Generate the lockfile to the latest ai4rag release before building.
uv lock --upgrade-package ai4rag

make refresh-certs
make build-openshell
make deploy-openshell
```

### Gateway registration and certificates

`make deploy-openshell` needs a working mTLS connection to the cluster gateway.
If a command reports `Unknown gateway 'openshift'` or that the gateway is not
registered or reachable, refresh the client certificates and the registration:

```bash
make refresh-certs
```

This target pulls the client certificates from the `openshell-client-tls` secret
and registers the `openshell-gateway` route as gateway `openshift`. Re-run it
after the cluster gateway is recreated or the certificates are rotated.
The gateway and its managed Sandboxes live in `openshell` by default. Image
builds, connection secrets, and `agent-client-token` use the current `oc` project.
The Sandbox resource is `default--<sandbox-name>` in the gateway namespace;
the standalone kit defaults to `rag-sandbox`, while the demo runner selects
`city-services-rag` through `OPENSHELL_SANDBOX_NAME`.

This prepared Makefile adds `OPENSHELL_NAMESPACE` (default `openshell`) for
standalone use with another existing gateway. The original v0.25.4 template
hardcodes `openshell`; it does not ask the end user to fill an empty namespace.
Changing this variable or the current project does not relocate a gateway-managed
Sandbox. The demo runner's complete flow currently supports `openshell` only.
Pass `OPENSHELL_GATEWAY_NAME=<name>` or `OPENSHELL_CLI_STATE=<directory>` to `make`
when using another registration name or host state directory. Use the same settings
as the administrator's gateway setup. The gateway must be compatible with the
downstream RHOAI CLI; an upstream 0.0.99 gateway needs migration by its administrator.

`make deploy-openshell` creates and configures the sandbox, starts the agent,
and exposes it through an OpenShift route.
It checks gateway connectivity before replacing an existing sandbox. The generated
agent startup enables a local CONNECT proxy for Milvus gRPC and native database
connections under OpenShell's egress policies.

To remove the sandbox and the agent route, run `make undeploy-openshell`. The
cluster gateway, the images, and the other cluster prerequisites are left
untouched.

The generated `agent_config.json` stays in the starter kit and is injected into
the sandbox during deployment. You can edit values such as `temperature` or
`system_message` and rerun `make deploy-openshell` without rebuilding the image.
Rebuild the image only after changing application code or dependencies.

The kit requests client/reviewer service-account tokens valid for 168 hours;
cluster policy may shorten that lifetime. Redeploy before expiry and update any
client configuration with the newly issued agent-client token. In the demo app,
restart the backend after updating `app-config.json`.

In Agent Ops, select the gateway namespace or All projects. The current kit omits
the Sandbox's `opendatahub.io/agent-framework: langchain` annotation, so Framework
may be blank. The generator fix is tracked in
[RHAI-19949](https://redhat.atlassian.net/browse/RHAI-19949); until then, annotate
the actual Sandbox after deployment if that metadata is needed.

## API and Swagger

After deployment, the Swagger/OpenAPI documentation is available at
`https://<agent-route>/docs`.

The Responses API can be called at `https://<agent-route>/v1/responses`:

```bash
# Run in the project containing the kit's agent-client-token secret.
TOKEN="$(oc get secret agent-client-token -o jsonpath='{.data.token}' | base64 -d)"
curl --fail-with-body --silent --show-error \
  --cacert "$HOME/.config/openshell/gateways/openshift/mtls/ca.crt" \
  'https://YOUR-AGENT-ROUTE/v1/responses' \
  -H "X-Api-Key: $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"input":"What guidance is available in this collection?","stream":false}'
```

Adjust the CA path if `OPENSHELL_CLI_STATE` or the registration name differs.
The API key is the agent-client token, separate from the MaaS API key and your
OpenShift login token. Answers may contain no structured citations; verify the
answer against the intended collection before use.

Additional optimized patterns are available in the RAG patterns artifact:

```text
s3://<bucket>/<run-id>/rag-templates-optimization/<artifact-id>/rag_patterns/
```
