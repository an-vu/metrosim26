"""Data files: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from metrosim26.workers import (
    _json_default,
)


def atomic_json(path, data, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf8") as out:
        if compact:
            # dumps uses the C encoder for unindented JSON; schema/content unchanged.
            out.write(
                json.dumps(
                    data,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=_json_default,
                    allow_nan=False,
                )
            )
        else:
            json.dump(data, out, sort_keys=True, indent=2, default=_json_default, allow_nan=False)
        out.write("\n")
    os.replace(tmp, path)


def atomic_npz(path, **arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as out:
        np.savez_compressed(out, **arrays)
    os.replace(tmp, path)


def _data_signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class InputFile:
    """One canonical expected input; transport URL is not its cache identity."""

    label: str
    kind: str
    path: Path
    url: str
    state: str = ""
    query: str = ""
    tile_id: int = -1
