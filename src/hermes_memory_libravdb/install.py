"""Install the libravdb plugin into the Hermes directory structure.

Hermes 0.14 discovers memory plugins by scanning:

    $HERMES_HOME/plugins/<name>/

This module creates that directory with thin re-exports to the pip-installed
package so ``hermes memory setup`` lists libravdb and ``hermes libravdb``
CLI commands are auto-discovered.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _hermes_home() -> Path:
    import os
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _plugin_dir() -> Path:
    return _hermes_home() / "plugins" / "libravdb"


def _init_py() -> str:
    return """\
\"\"\"LibraVDB memory provider plugin for Hermes Agent.\"\"\"
from hermes_memory_libravdb import register, LibraVDBMemoryProvider  # noqa: F401
"""


def _cli_py() -> str:
    return """\
\"\"\"CLI commands for LibraVDB memory provider.\"\"\"
from hermes_memory_libravdb.cli import register_cli, libravdb_command  # noqa: F401
"""


def is_installed() -> bool:
    """Return True if the plugin directory already exists."""
    d = _plugin_dir()
    return d.is_dir() and (d / "__init__.py").exists() and (d / "cli.py").exists()


def install(force: bool = False) -> bool:
    """Create the Hermes plugin directory with thin re-export wrappers.

    Returns True on success or if already installed.
    """
    target = _plugin_dir()

    if target.is_dir() and not force:
        if is_installed():
            return True

    target.mkdir(parents=True, exist_ok=True)

    (target / "__init__.py").write_text(_init_py())
    (target / "cli.py").write_text(_cli_py())

    # Copy plugin.yaml for metadata discovery
    import hermes_memory_libravdb
    pkg_dir = Path(hermes_memory_libravdb.__path__[0])
    yaml_src = pkg_dir.parent.parent / "plugin.yaml"
    if yaml_src.exists():
        (target / "plugin.yaml").write_text(yaml_src.read_text())

    return True


def uninstall() -> bool:
    """Remove the Hermes plugin directory."""
    import shutil
    target = _plugin_dir()
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False


def status() -> dict:
    """Return install status information."""
    target = _plugin_dir()
    return {
        "installed": is_installed(),
        "plugin_dir": str(target),
        "hermes_home": str(_hermes_home()),
        "exists": target.is_dir(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Install LibraVDB memory plugin for Hermes Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="Install to $HERMES_HOME/plugins/libravdb/")
    sub.add_parser("uninstall", help="Remove from $HERMES_HOME/plugins/libravdb/")
    sub.add_parser("status", help="Show install status")

    args = parser.parse_args()

    if args.command == "install":
        if install():
            print(f"Installed to {_plugin_dir()}")
        else:
            print("Already installed")
    elif args.command == "uninstall":
        if uninstall():
            print(f"Removed {_plugin_dir()}")
        else:
            print("Nothing to uninstall")
    elif args.command == "status":
        import json
        print(json.dumps(status(), indent=2))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
