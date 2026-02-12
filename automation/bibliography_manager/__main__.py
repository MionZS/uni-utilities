"""Entry point: python -m automation.bibliography-manager [--path PATH]"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TCC Bibliography Manager — TUI for research paper tracking",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Path to the bibliography JSON file (default: bibliography/data.json)",
    )
    args = parser.parse_args()

    from .app import BibliographyApp

    app = BibliographyApp(bib_path=args.path)
    app.run()


if __name__ == "__main__":
    main()
