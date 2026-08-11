"""manifest.json 依赖声明测试。"""

from __future__ import annotations

import json
from pathlib import Path


def test_manifest_python_dependencies_pin_280() -> None:
    """manifest python_dependencies 精确锁定 2.8.0。"""
    manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    deps = manifest.get("python_dependencies", [])
    assert "maibot-plugin-sdk==2.8.0" in deps


def test_manifest_description_does_not_mention_271() -> None:
    """manifest description 不再引用 2.7.1。"""
    manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    description = manifest.get("description", "")
    assert "2.7.1" not in description
    assert "2.8.0" in description