"""Verify compile traversal is owned by the shared inventory."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
INVENTORY = ROOT / "src/apm_cli/compilation/inventory.py"
OPTIMIZER = ROOT / "src/apm_cli/compilation/context_optimizer.py"
DISCOVERY = ROOT / "src/apm_cli/primitives/discovery.py"
DISTRIBUTED = ROOT / "src/apm_cli/compilation/distributed_compiler.py"
AGENTS = ROOT / "src/apm_cli/compilation/agents_compiler.py"


def _has_all(source: str, required: tuple[str, ...]) -> bool:
    """Return whether every required contract fragment appears in source."""
    return all(fragment in source for fragment in required)


def main() -> int:
    """Return nonzero when compile traversal has a duplicate authority."""
    inventory = INVENTORY.read_text(encoding="utf-8")
    optimizer = OPTIMIZER.read_text(encoding="utf-8")
    discovery = DISCOVERY.read_text(encoding="utf-8")
    distributed = DISTRIBUTED.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")

    valid = (
        inventory.count("class CompileInventory") == 1
        and inventory.count("os.walk(") == 1
        and _has_all(
            inventory,
            (
                'if path != root and (".git" in file_names or ".git" in child_dirs):',
                "nested_repository_roots.add(path)",
                "def nested_repository_root_for(",
            ),
        )
        and "os.walk(" not in optimizer
        and "os.walk(" not in distributed
        and "os.walk(" not in discovery
        and _has_all(
            optimizer,
            (
                "from .inventory import CompileInventory",
                "inventory = self._inventory or CompileInventory.collect(self.base_dir)",
                "inventory.files_under(self._scan_top_level_roots)",
            ),
        )
        and _has_all(
            discovery,
            (
                "inventory: CompileInventory | None = None",
                "inventory = CompileInventory.collect(base_path, exclude_patterns=exclude_patterns)",
                "inventory.files_within(base_path)",
                "inventory.nested_repository_root_for(directory)",
            ),
        )
        and _has_all(
            distributed,
            (
                "source_inventory: CompileInventory | None = None",
                "deploy_inventory: CompileInventory | None = None",
                "deploy_inventory.nested_repository_root_for(directory_path)",
                "for directory_path, (relative_path, files) in sorted(cleanup_directories.items()):",
            ),
        )
        and _has_all(
            agents,
            (
                "self._source_inventory = CompileInventory.collect(",
                "self.source_dir, exclude_patterns=config.exclude",
                "source_inventory=self._source_inventory",
                "deploy_inventory=self._deploy_inventory",
                "deploy_inventory.nested_repository_root_for(agents_path.parent)",
            ),
        )
        and "_nested_git_repository_root" not in agents
        and ' / ".git"' not in agents
        and ' / ".git"' not in distributed
        and ' / ".git"' not in discovery
    )
    if valid:
        return 0

    print("[x] Compile nested Git boundaries must route through compilation/inventory.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
