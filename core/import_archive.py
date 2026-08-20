#!/usr/bin/env python3
"""Utilities for preserving exact copies of files consumed by the desktop app."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return text.strip("._-") or "file"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_imports(workspace, stage: str, files):
    """Copy input files into a timestamped workspace archive.

    Args:
        workspace: Tournament workspace folder.
        stage: Human-readable stage name, e.g. ``payment_check``.
        files: Iterable of ``(role, path)`` pairs.

    Returns:
        Path to the created archive batch folder.

    The copies are byte-for-byte copies (metadata is retained where the OS
    allows it).  A manifest is written beside them with original locations,
    sizes, and SHA-256 hashes so an event can later be reconstructed exactly.
    """
    workspace = Path(workspace).expanduser()
    archive_root = workspace / "imported_files"
    archive_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H%M%S_%f")[:-3]
    batch = archive_root / f"{timestamp}_{_safe_name(stage)}"
    suffix = 2
    while batch.exists():
        batch = archive_root / f"{timestamp}_{_safe_name(stage)}_{suffix}"
        suffix += 1
    batch.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schema_version": 1,
        "stage": stage,
        "archived_at": now.isoformat(timespec="seconds"),
        "files": [],
    }

    seen_destinations = set()
    for role, raw_path in files:
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Cannot archive missing input file: {source}")

        role_name = _safe_name(role)
        base_name = _safe_name(source.name)
        destination_name = f"{role_name}__{base_name}"
        stem = Path(destination_name).stem
        suffix_text = Path(destination_name).suffix
        counter = 2
        while destination_name.casefold() in seen_destinations or (batch / destination_name).exists():
            destination_name = f"{stem}_{counter}{suffix_text}"
            counter += 1
        seen_destinations.add(destination_name.casefold())

        destination = batch / destination_name
        shutil.copy2(source, destination)
        checksum = _sha256(destination)

        manifest["files"].append(
            {
                "role": str(role),
                "original_path": str(source),
                "archived_filename": destination.name,
                "size_bytes": destination.stat().st_size,
                "sha256": checksum,
            }
        )

    (batch / "import_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return batch
