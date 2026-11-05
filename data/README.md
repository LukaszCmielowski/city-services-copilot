# Input data layout

Synthetic fixtures are included so validation and S3 upload work immediately: 5,000 tabular classification rows, 7,300 daily time-series rows across every service/neighborhood combination, and a twenty-two-document AutoRAG demo corpus. The risk fixture intentionally excludes post-closure fields such as `resolution_days` to prevent target leakage. The guidance files are short, original demo summaries with official source links. Do not commit data containing request addresses, reporter details, or other sensitive fields.

```text
data/
├── sample-requests.json              # five fixed reports shown in the app
├── prepared/
│   ├── resolution-risk.csv          # AutoML tabular input
│   └── service-demand-daily.csv     # AutoML time-series input
├── guidance/                        # original demo summaries with source links
│   └── *.md                          # 22 bundled guidance documents
└── autorag-evaluation.json
```

The backend reads the request queue, demand history, and sample guidance locally. Live forecasts use the bundled history timestamps; the UI shows relative Day 1–Day 7 labels. The fixture dates do not need an annual update. `service_catalog.py` defines shared service targets, synthetic risk factors, and guidance filenames.

Use the cluster runner to upload both prepared CSVs, the guidance corpus, and the evaluation JSON together. Once those objects exist at the configured bucket and keys, `--skip-upload` reuses them for new pipeline runs. Deploying an existing successful run does not upload data or re-index its collection. See the root [setup guide](../README.md#rerun-without-uploading-data) for commands.

Regenerate the CSVs with `python3 scripts/generate_demo_fixtures.py` only when deliberately changing the training fixtures. Changed training data requires new AutoML runs; a changed corpus requires the separate indexing workflow before serving from it. Restarting or reviewing a request in the app requires neither.
