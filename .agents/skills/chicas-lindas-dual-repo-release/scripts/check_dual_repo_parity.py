from __future__ import annotations

import argparse
from pathlib import Path


def branch(repo: Path) -> str:
    head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else "DETACHED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace_root.resolve()
    repos = [root / "barcebot", root / "replaybot", root / "billing-control-plane"]
    failures: list[str] = []
    expected = "feat/monthly-billing-entitlements"

    for repo in repos:
        if not (repo / ".git").exists():
            failures.append(f"not a Git repository: {repo}")
            continue
        current = branch(repo)
        if current != expected:
            failures.append(f"{repo.name}: branch is {current!r}, expected {expected!r}")

    for filename in ("billing_entitlement.py", "billing_entitlement.mjs"):
        left = root / "barcebot" / filename
        right = root / "replaybot" / filename
        if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
            failures.append(f"billing client parity failed: {filename}")

    for filename in (
        "wa_incident_monitor.mjs",
        "wa_incident_health.py",
        "wa_disconnect_policy.mjs",
        "wa_delivery_safety.mjs",
    ):
        left = root / "barcebot" / filename
        right = root / "replaybot" / filename
        if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
            failures.append(f"WhatsApp incident parity failed: {filename}")

    for repo_name in ("barcebot", "replaybot"):
        repo = root / repo_name
        requirements = (repo / "requirements.txt").read_text(encoding="utf-8")
        entrypoint = (repo / "entrypoint.sh").read_text(encoding="utf-8")
        if "cryptography" not in requirements:
            failures.append(f"{repo_name}: cryptography dependency missing")
        if "BILLING_CONTROL_PLANE_ADMIN_TOKEN" not in entrypoint:
            failures.append(f"{repo_name}: admin token is not stripped from workers")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print("Branches, billing clients, WhatsApp incident modules, dependencies, and worker secret boundaries match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
