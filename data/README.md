# Input data layout

Synthetic, sanitized fixtures are included so the validation and S3-upload commands work immediately: 5,000 tabular classification rows, 7,300 daily time-series rows across every service/neighborhood combination, and a twenty-two-document AutoRAG demo corpus. The risk fixture intentionally excludes post-closure fields such as `resolution_days` to prevent target leakage. The guidance files are short, original demo summaries with official source links. Recreate the CSVs with `python3 scripts/generate_demo_fixtures.py` if needed. Do not commit data containing request addresses, reporter details, or other sensitive fields.

```text
data/
├── prepared/
│   ├── resolution-risk.csv          # AutoML tabular input
│   └── service-demand-daily.csv     # AutoML time-series input
├── guidance/                        # official PDF, DOCX, MD, HTML, or TXT files
│   └── *.md                          # 22 bundled guidance documents
└── autorag-evaluation.json
```

Use these locations as staging only. Upload `prepared/` and `guidance/` to an S3-compatible bucket connected to your OpenShift AI project. See `../README.md` for the required schemas and setup steps.
