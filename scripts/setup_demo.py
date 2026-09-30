#!/usr/bin/env python3
"""Interactive setup wizard for the City Services Copilot demo."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ask(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{label}{suffix}: ").strip() or default
        if answer or not required:
            return answer
        print("A value is required.")


def yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def run(command: list[str]) -> int:
    print("\n$ " + " ".join(command))
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def print_pipeline_parameters(bucket: str, prefix: str, automl_secret: str, test_secret: str, docs_secret: str, maas_secret: str, db_secret: str, embedding_models: str, generation_models: str) -> None:
    print("\n--- 1. AutoML tabular: autogluon-tabular-training-pipeline ---")
    print(f"train_data_secret_name = {automl_secret}")
    print(f"train_data_bucket_name = {bucket}")
    print(f"train_data_file_key = {prefix}/automl/resolution-risk.csv")
    print("label_column = sla_miss\ntask_type = binary\npositive_class = 1\ntop_n = 3\npreset = speed")
    print("\n--- 2. AutoML time series: autogluon-timeseries-training-pipeline ---")
    print(f"train_data_secret_name = {automl_secret}")
    print(f"train_data_bucket_name = {bucket}")
    print(f"train_data_file_key = {prefix}/automl/service-demand-daily.csv")
    print("target = target\ntimestamp_column = timestamp\nid_column = item_id\nprediction_length = 7\ntop_n = 3\npreset = speed")
    print("\n--- 3. AutoRAG: documents-rag-optimization-pipeline ---")
    print(f"test_data_secret_name = {test_secret}\ntest_data_bucket_name = {bucket}\ntest_data_key = {prefix}/autorag/autorag-evaluation.json")
    print(f"input_data_secret_name = {docs_secret}\ninput_data_bucket_name = {bucket}\ninput_data_keys = ['{prefix}/autorag/guidance/']")
    print(f"maas_secret_name = {maas_secret}\ndb_secret_name = {db_secret}")
    print(f"embedding_models = {embedding_models}\ngeneration_models = {generation_models}")
    print("optimization_metric = unitxt:faithfulness\noptimization_max_rag_patterns = 5\npreset = speed")


def main() -> None:
    print("City Services Copilot setup wizard\n")
    print("This wizard uses the small bundled fixtures. It never asks for or stores secret values.")
    print("You need existing Kubernetes secret names and S3/MaaS/database connections from your platform administrator.\n")

    if run([sys.executable, "scripts/validate_demo_inputs.py"]) != 0:
        print("\nFix validation errors before continuing.", file=sys.stderr)
        raise SystemExit(1)

    bucket = ask("S3-compatible bucket name", required=True)
    prefix = ask("Object-key prefix", "city-services-copilot", required=True).strip("/")
    profile = ask("AWS CLI profile (leave blank for default)")
    endpoint = ask("S3 endpoint URL (leave blank for AWS S3)")
    automl_secret = ask("AutoML S3 Kubernetes secret name", required=True)
    test_secret = ask("AutoRAG evaluation-data S3 secret name", automl_secret, required=True)
    docs_secret = ask("AutoRAG documents S3 secret name", automl_secret, required=True)
    maas_secret = ask("MaaS Kubernetes secret name", required=True)
    db_secret = ask("Milvus or PGVector Kubernetes secret name", required=True)
    embedding_models = ask("Embedding model IDs as a KFP list", "['your-embedding-model']", required=True)
    generation_models = ask("Generation model IDs as a KFP list", "['your-generation-model']", required=True)

    if yes_no(f"\nUpload bundled demo fixtures to s3://{bucket}/{prefix}/ now?", default=False):
        if not shutil.which("aws"):
            print("AWS CLI was not found. Install/configure it, then re-run this wizard.", file=sys.stderr)
            raise SystemExit(1)
        upload = ["bash", "scripts/upload_demo_data.sh", "--bucket", bucket, "--prefix", prefix, "--apply"]
        if profile:
            upload.extend(["--profile", profile])
        if endpoint:
            upload.extend(["--endpoint-url", endpoint])
        if run(upload) != 0:
            print("\nUpload failed. Check the AWS profile, endpoint, bucket permission, and input files.", file=sys.stderr)
            raise SystemExit(1)
    else:
        print("\nSkipped upload. Run the displayed upload command later, or re-run this wizard.")

    print_pipeline_parameters(bucket, prefix, automl_secret, test_secret, docs_secret, maas_secret, db_secret, embedding_models, generation_models)
    print("\nNext: create the three runs in your pipeline dashboard using the values above.")
    print("After selecting your artifacts, start the local app with: python3 main.py")
    print("Full guidance: README.md")


if __name__ == "__main__":
    main()
