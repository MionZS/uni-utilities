"""Entry point: python -m automation.bibliography_manager [--path PATH] [--external]"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import warnings


_DEFAULT_COLS = 130
_DEFAULT_ROWS = 40


def _launch_external(extra_args: list[str]) -> int:
    """Re-launch the TUI in an external cmd.exe window with a fixed size."""
    cols = _DEFAULT_COLS
    rows = _DEFAULT_ROWS
    py = sys.executable
    module_args = [py, "-m", "automation.bibliography_manager"] + extra_args
    cmd_inner = " && ".join([
        f"mode con: cols={cols} lines={rows}",
        " ".join(module_args),
        "pause",
    ])
    return subprocess.call(
        ["cmd", "/c", f"start \"Bibliography Manager\" cmd /k \"{cmd_inner}\""],
        shell=False,
    )


def main() -> None:
    # Suppress harmless asyncio pipe cleanup warnings on Windows / Python 3.14+
    warnings.filterwarnings("ignore", message="unclosed transport", category=ResourceWarning)

    parser = argparse.ArgumentParser(
        description="TCC Bibliography Manager — TUI for research paper tracking",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Path to the bibliography JSON file (default: bibliography/data.json)",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help=f"Launch in an external terminal window ({_DEFAULT_COLS}x{_DEFAULT_ROWS})",
    )
    args = parser.parse_args()

    if args.external:
        # Build arg list for the inner invocation (without --external)
        inner_args: list[str] = []
        if args.path:
            inner_args += ["--path", args.path]
        raise SystemExit(_launch_external(inner_args))

    # Attempt to resize the current console on Windows
    if os.name == "nt":
        try:
            os.system(f"mode con: cols={_DEFAULT_COLS} lines={_DEFAULT_ROWS}")
        except Exception:
            pass

    from .app import BibliographyApp

    app = BibliographyApp(bib_path=args.path)
    app.run()


if __name__ == "__main__":
    main()
