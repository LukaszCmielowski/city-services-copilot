# Prepared downstream runtime

Based on deployment templates from IBM/ai4rag, Apache-2.0, commit e9472f1b72d9486c23f07f6390b55dac66468fe9 (release v0.25.4).

https://github.com/IBM/ai4rag/tree/e9472f1b72d9486c23f07f6390b55dac66468fe9/ai4rag/assets_generator/starter_kit_templates/agentic_rag

`Makefile`, `Containerfile.openshell`, `proxychains_entrypoint.py`, and `README.md`
define the single supported downstream deployment runtime in this repository.
They are prepared for every deployment, independently of the downloaded kit's deployment file versions.
Generated agent configuration, application code, dependency declarations, and connection names remain from the run.
The original files are retained under deployment workspace/original-runtime/.

These are locally maintained deployment files, not byte-for-byte copies of the
upstream templates. Local adjustments include:

- `OPENSHELL_BUILD_PULL_SECRET` explicitly sets the Docker BuildConfig pullSecret
  for multi-stage base image pulls. The runner selects an existing builder
  service-account registry secret, or an explicit project secret.
- `OPENSHELL_NAMESPACE` defaults to `openshell` and makes the standalone Makefile's
  gateway resource lookups configurable. The demo runner still expects `openshell`.
- Containerized CLI image selection includes the RHOAI CSV image and a matching
  Quay fallback; gateway registration uses the mounted local mTLS state.
- The README describes the prepared runtime, prerequisites, verified TLS,
  namespace ownership, token lifetime, and current Agent Ops metadata gap.
- The unimplemented `run-cli` placeholder target is omitted; local agent
  development uses the working `run-app` target.

Updating this README does not modify the upstream generator or any previously
downloaded deployment workspace. New deployments receive the bundled files.
