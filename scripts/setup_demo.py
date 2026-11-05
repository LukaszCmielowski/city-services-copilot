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
    print("\n$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def confirm(action: str) -> bool:
    answer = input(f"\n{action}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--apply", action="store_true", help="run without an interactive confirmation")
    parser.add_argument("--dry-run", action="store_true", help="validate and show the submission or deployment plan without cluster writes")
    parser.add_argument("--skip-upload", action="store_true", help="submit runs without uploading bundled data")
    parser.add_argument("--only", choices=("all", "tabular", "timeseries", "autorag"), default="all")
    parser.add_argument("--wait", action="store_true", help="wait for each submitted KFP run to finish")
    parser.add_argument("--show-parameters", action="store_true", help="include full KFP parameter JSON in the preflight")
    parser.add_argument("--deploy-autorag", metavar="RUN_ID", help="deploy a completed AutoRAG run's starter kit instead of submitting pipelines")
    parser.add_argument("--deploy", metavar="RUN_ID", nargs="+", help="deploy completed AutoML runs, wait, test scoring, and configure the app")
    parser.add_argument("--auto-deploy", action="store_true", help="wait for and deploy every selected pipeline, test endpoints, and configure the app")
    parser.add_argument("--auto-deploy-automl", action="store_true", help="wait for and deploy selected AutoML runs and configure live scoring")
    parser.add_argument("--auto-deploy-autorag", action="store_true", help="wait for AutoRAG, build/deploy its starter kit, and configure live guidance")
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
    if args.show_parameters:
        runner.append("--show-parameters")
    if args.deploy_autorag:
        runner.extend(["--deploy-autorag", args.deploy_autorag])
    if args.deploy:
        runner.extend(["--deploy", *args.deploy, "--wait-deploy"])
    if args.auto_deploy:
        runner.append("--auto-deploy")
    if args.auto_deploy_automl:
        runner.append("--auto-deploy-automl")
    if args.auto_deploy_autorag:
        runner.append("--auto-deploy-autorag")

    print("City Services Copilot setup\n")
    print("This guided flow uses the bundled synthetic data and your existing cluster connections.")
    if args.deploy_autorag or (
        args.only in {"all", "autorag"} and (args.auto_deploy or args.auto_deploy_autorag)
    ):
        print("AutoRAG deployment requires a preinstalled Red Hat gateway in openshell and running local Podman.")
        print("It prepares the downstream kit, refreshes gateway authentication, and creates serving resources and agent tokens.")
    if run([*runner, "--dry-run"]) != 0:
        print("\nResolve the preflight error above before continuing. Use actual run IDs in deployment commands.", file=sys.stderr)
        raise SystemExit(1)

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to execute the plan.")
        return
    action = "Submit the selected pipeline runs" if args.skip_upload else "Upload the bundled data and submit the selected pipeline runs"
    if args.deploy:
        action = "Deploy the selected AutoML models, wait for readiness, test scoring, and configure the app"
        if args.deploy_autorag:
            action += ", then deploy AutoRAG and configure live guidance"
    elif args.deploy_autorag:
        action = "Build and deploy the selected AutoRAG starter kit and configure live guidance"
    elif args.auto_deploy:
        action += ", then deploy all selected models, test endpoints, and configure the app"
    elif args.auto_deploy_automl:
        action += ", then deploy AutoML and configure live scoring"
        if args.auto_deploy_autorag:
            action += " and AutoRAG guidance"
    elif args.auto_deploy_autorag:
        action += ", then build/deploy AutoRAG and configure live guidance"
    if not args.apply and not confirm(action):
        print("No changes were made.")
        return
    raise SystemExit(run(runner))


if __name__ == "__main__":
    main()
