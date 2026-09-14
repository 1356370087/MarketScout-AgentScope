"""Import archives are external input; validate structure before scheduling work."""

import io
import json
from zipfile import ZipFile

import pytest

from open_deep_research.knowledge.exporter import (
    MANIFEST_FORMAT,
    TABLES,
    precheck_import,
)


def archive(manifest, extra=None):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for key, value in (extra or {}).items():
            z.writestr(key, value)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "manifest", [[], None, {"format": "old"}, {"format": MANIFEST_FORMAT, "tables": {}}]
)
def test_invalid_manifest_is_reported(manifest):
    assert precheck_import(archive(manifest))["ok"] is False


def test_checksum_and_path_tampering():
    manifest = {
        "format": MANIFEST_FORMAT,
        "tables": {t: [] for t in TABLES},
        "files": {},
    }
    assert precheck_import(archive(manifest))["ok"] is True
    assert not precheck_import(archive(manifest, {"../escape": "x"}))["ok"]
    manifest["files"] = {"originals/a": {"size": 1, "sha256": "0" * 64}}
    assert not precheck_import(archive(manifest, {"originals/a": "x"}))["ok"]


def test_broken_evidence_reference():
    manifest = {
        "format": MANIFEST_FORMAT,
        "tables": {t: [] for t in TABLES},
        "files": {},
    }
    manifest["tables"]["knowledge_fact_evidence"] = [
        {
            "id": "aa89ba27-ef08-4d46-80a7-066ed7a1e09b",
            "generation_id": "14211687-3fb1-4b81-ba71-cbdf9e76e8a9",
        }
    ]
    assert not precheck_import(archive(manifest))["ok"]
