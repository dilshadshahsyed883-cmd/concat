"""Named SDR looks shared with the native Concat filter catalogue."""
from __future__ import annotations

import json
from pathlib import Path
from .filters import FilterSettings

LOOK_CATALOGUE = tuple(json.loads(Path(__file__).with_name("looks_catalogue.json").read_text(encoding="utf-8")))
_FIELDS = ("brightness", "contrast", "saturation", "temperature", "sharpen", "exposure")
FILTER_LOOKS = {
    item["id"]: FilterSettings(**{field: item[field] for field in _FIELDS})
    for item in LOOK_CATALOGUE
}
if len(FILTER_LOOKS) != len(LOOK_CATALOGUE):
    raise ValueError("Duplicate filter look id")
