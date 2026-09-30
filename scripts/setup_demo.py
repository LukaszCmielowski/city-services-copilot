#!/usr/bin/env python3
"""Interactive front end for the .env-driven City Services Copilot cluster runner.

Run ``python3 scripts/setup_demo.py`` after copying .env.example to .env and
filling in the required cluster and connection values.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(command: list[str]) -> int:
    print("\n$ " + " ".join(command))
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def confirm(action: str) -> bool:
    answer = input(f"\n{action}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--apply", action="store_true", help="run without an interactive confirmation")
    parser.add_argument("--dry-run", action="store_true", help="only show the planned upload and submissions")
    parser.add_argument("--skip-upload", action="store_true", help="submit runs without uploading bundled data")
    parser.add_argument("--only", choices=("all", "tabular", "timeseries", "autorag"), default="all")
    parser.add_argument("--wait", action="store_true", help="wait for each submitted KFP run to finish")
    args = parser.parse_args()

    if not args.env_file.is_file():
        print(f"Environment file not found: {args.env_file}", file=sys.stderr)
        print("Create it with: cp .env.example .env", file=sys.stderr)
        raise SystemExit(1)

    runner = [sys.executable, "scripts/run_cluster_demo.py", "--env-file", str(args.env_file)]
    if args.skip_upload:
        runner.append("--skip-upload")
    if args.only != "all":
        runner.extend(["--only", args.only])
    if args.wait:
        runner.append("--wait")
    if args.apply:
        runner.append("--non-interactive")

    print("City Services Copilot setup\n")
    print("This guided flow uses the bundled synthetic data and your existing cluster connections.")
    print("It never creates Kubernetes secrets or downloads external datasets.")
    if run([*runner, "--dry-run"]) != 0:
        print("\nFix the reported .env or bundled-data issue before continuing.", file=sys.stderr)
        raise SystemExit(1)

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to upload and submit runs.")
        return
    action = "Submit the selected pipeline runs" if args.skip_upload else "Upload the bundled data and submit the selected pipeline runs"
    if not args.apply and not confirm(action):
        print("No changes were made.")
        return
    raise SystemExit(run(runner))


if __name__ == "__main__":
    main()
