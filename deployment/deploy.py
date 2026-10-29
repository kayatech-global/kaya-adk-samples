"""Plan, and on request carry out, a deployment of the agent (`adk deploy` also works on the agent folder).

Run from the repository root:

    python deployment/deploy.py list
    python deployment/deploy.py docker --set image=registry.example/kyc:1 [--apply]
    python deployment/deploy.py kubernetes --set image=registry.example/kyc:1 --set namespace=agents
    python deployment/deploy.py cloud_run --set project=my-project --set region=europe-west1

Without `--apply` a target only writes its files under `build/deploy/<target>/` and prints the commands it would run.
A target is a `DeployTarget` registered with `register_target` (both in `retail_kyc_onboarding._engine.deploy`); define your
own in this file to add a platform.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]  # the repository root
PACKAGE = "retail_kyc_onboarding"
AGENT_DIR = ROOT / PACKAGE  # the agent folder: the Docker build context and what `adk deploy` ships
sys.path.insert(0, str(ROOT))

from retail_kyc_onboarding._engine.deploy import DeployProject, target, target_names  # noqa: E402
from retail_kyc_onboarding._engine.errors import ConfigError  # noqa: E402


def project() -> DeployProject:
    """The agent as a deploy target sees it."""
    from retail_kyc_onboarding.workflow import SECRETS, WORKFLOW

    identity = WORKFLOW.identity
    return DeployProject(
        root=AGENT_DIR,
        package=PACKAGE,
        export_id=identity.export.id,
        export_version=identity.export.version,
        adk_version=identity.target.adk,
        secret_env=tuple(s.name.upper() for s in SECRETS),
    )


def _settings(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got '{pair}'")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the deploy command line; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="deploy.py", description="Plan or carry out a deployment of the agent.")
    parser.add_argument("target", help="a target name, or 'list'")
    parser.add_argument("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE", help="a target setting")
    parser.add_argument("--out", type=Path, help="where to write the target's files (default: build/deploy/<target>)")
    parser.add_argument("--apply", action="store_true", help="also run the commands (build, push, deploy)")
    args = parser.parse_args(argv)

    if args.target == "list":
        for name, summary in target_names().items():
            print(f"{name:22} {summary}")
        return 0
    try:
        chosen = target(args.target, _settings(args.settings))
        out = (args.out or ROOT / "build" / "deploy" / args.target).resolve()
        plan = chosen.plan(project(), out)
    except (ConfigError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(plan.describe(out))
    if args.apply:
        chosen.apply(plan, out)
    else:
        chosen.write(plan, out)
        if plan.commands:
            print("dry run: files written, nothing run (add --apply to run the commands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
