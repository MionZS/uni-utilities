"""Atomic read / write for the bibliography JSON file.

All writes go to a temporary file first, then are renamed into place
so a crash mid-write never corrupts the real file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Bibliography

DEFAULT_PATH = Path("bibliography") / "data.json"

# Safety limit: refuse to load JSON files larger than 100 MB.
# If you hit this, consider migrating to SQLite.
_MAX_JSON_BYTES = 100 * 1024 * 1024


def resolve_path(path: str | Path | None = None) -> Path:
    """Return an absolute Path, falling back to DEFAULT_PATH."""
    p = Path(path) if path else DEFAULT_PATH
    return p.expanduser().resolve()


def load(path: str | Path | None = None) -> Bibliography:
    """Read and validate the JSON file.  Returns empty Bibliography if missing."""
    p = resolve_path(path)
    if not p.exists():
        return Bibliography()

    size = p.stat().st_size
    if size > _MAX_JSON_BYTES:
        raise RuntimeError(
            f"Bibliography file is {size / 1024 / 1024:.1f} MB, "
            f"exceeding the {_MAX_JSON_BYTES / 1024 / 1024:.0f} MB safety limit. "
            "Consider migrating to SQLite."
        )

    raw = p.read_text(encoding="utf-8")
    return Bibliography.model_validate_json(raw)


def save(bib: Bibliography, path: str | Path | None = None) -> Path:
    """Atomically write *bib* to *path*.  Creates parent dirs if needed."""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    data = bib.model_dump_json(indent=2, exclude_none=True)

    # Write to temp file in the same directory, then replace atomically.
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp", prefix=".bib_")
    try:
        os.write(fd, (data + "\n").encode("utf-8"))
        os.close(fd)
        # os.replace() is atomic on the same filesystem and handles
        # Windows (overwrites existing) — no gap between unlink/rename.
        os.replace(tmp, p)
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None  # noqa: E501
        Path(tmp).unlink(missing_ok=True)
        raise

    return p
