Deployment templates from IBM/ai4rag, Apache-2.0, commit e9472f1b72d9486c23f07f6390b55dac66468fe9 (release v0.25.4).

https://github.com/IBM/ai4rag/tree/e9472f1b72d9486c23f07f6390b55dac66468fe9/ai4rag/assets_generator/starter_kit_templates/agentic_rag

These four files define the single supported downstream deployment runtime.
They are prepared for every deployment, independently of the downloaded kit's deployment file versions.
Generated agent configuration, application code, dependency declarations, and connection names remain from the run.
The original files are retained under deployment workspace/original-runtime/.

Local Makefile addition: OPENSHELL_BUILD_PULL_SECRET explicitly sets the Docker
BuildConfig pullSecret for multi-stage base image pulls. The runner selects an
existing builder service-account registry secret, or an explicit project secret.
