#!/usr/bin/env bash
# Upload prepared City Services Copilot data using the configured AWS CLI.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/upload_demo_data.sh --bucket BUCKET [options]

Required:
  --bucket BUCKET       Target S3-compatible bucket name.

Options:
  --prefix PREFIX       Object prefix (default: city-services-copilot).
  --profile PROFILE     AWS CLI profile to use.
  --endpoint-url URL    S3-compatible endpoint passed to AWS CLI.
  --apply               Perform uploads. Without this, only a dry run is performed.
  --skip-validation     Do not run scripts/validate_demo_inputs.py first.
  -h, --help            Show this help.

The script uploads the bundled prepared AutoML data, guidance files, and
AutoRAG evaluation data.
EOF
}

bucket=""
prefix="city-services-copilot"
profile=""
endpoint_url=""
apply=false
validate=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket) bucket="${2:?missing bucket}"; shift 2 ;;
    --prefix) prefix="${2:?missing prefix}"; shift 2 ;;
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --endpoint-url) endpoint_url="${2:?missing endpoint URL}"; shift 2 ;;
    --apply) apply=true; shift ;;
    --skip-validation) validate=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$bucket" ]] || { echo "--bucket is required" >&2; usage >&2; exit 2; }
command -v aws >/dev/null || { echo "AWS CLI is required; install/configure it before upload." >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${prefix#/}"
prefix="${prefix%/}"

if [[ "$validate" == true ]]; then
  python3 "$root/scripts/validate_demo_inputs.py" \
    --risk "$root/data/prepared/resolution-risk.csv" \
    --forecast "$root/data/prepared/service-demand-daily.csv" \
    --evaluation "$root/data/autorag-evaluation.json" \
    --documents "$root/data/guidance" \
    --document-prefix "$prefix/autorag/guidance"
fi

aws_args=()
copy_args=()
[[ -n "$profile" ]] && aws_args+=(--profile "$profile")
[[ -n "$endpoint_url" ]] && aws_args+=(--endpoint-url "$endpoint_url")
[[ "$apply" == false ]] && copy_args+=(--dryrun)

echo "Target: s3://$bucket/$prefix/"
[[ "$apply" == false ]] && echo "Dry run only. Re-run with --apply to upload."

aws "${aws_args[@]}" s3 cp "$root/data/prepared/resolution-risk.csv" "s3://$bucket/$prefix/automl/resolution-risk.csv" "${copy_args[@]}"
aws "${aws_args[@]}" s3 cp "$root/data/prepared/service-demand-daily.csv" "s3://$bucket/$prefix/automl/service-demand-daily.csv" "${copy_args[@]}"
aws "${aws_args[@]}" s3 cp "$root/data/autorag-evaluation.json" "s3://$bucket/$prefix/autorag/autorag-evaluation.json" "${copy_args[@]}"
aws "${aws_args[@]}" s3 cp "$root/data/guidance" "s3://$bucket/$prefix/autorag/guidance/" --recursive "${copy_args[@]}"

echo "Upload step complete."
