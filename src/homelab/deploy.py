from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .output import print_action, print_ok, print_sub, print_warn


def prepare_build_dir(build_dir: Path) -> None:
    previous_dir = build_dir.with_name(f"{build_dir.name}.prev")
    if previous_dir.exists():
        shutil.rmtree(previous_dir)
    if build_dir.exists():
        build_dir.rename(previous_dir)
    build_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class DeploySession:
    module: str
    failed_hosts: list[str] = field(default_factory=list)

    def run(self, deploy_host: Callable[[str], None], hosts: list[str]) -> None:
        print_action(f"Deploying {self.module}")
        print_sub(f"Hosts: {' '.join(hosts)}")
        print()

        for host in hosts:
            print_action(f"Deploying to {host}...")
            try:
                deploy_host(host)
            except Exception as exc:
                print_warn(f"Failed to deploy to {host}: {exc}")
                self.failed_hosts.append(host)
            else:
                print_ok(f"Deployed to {host}")
            print()

    def finish(self) -> bool:
        print_action("Deployment complete!")
        if self.failed_hosts:
            print()
            print_warn(f"Failed hosts: {' '.join(self.failed_hosts)}")
            return False
        return True
