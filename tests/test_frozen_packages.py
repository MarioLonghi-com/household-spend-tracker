"""What the desktop backend says it is made of (#453).

PyInstaller bundles code without most dist-info, so the frozen build reads the
list its freeze wrote instead of importlib.metadata.
"""

from __future__ import annotations

import json

from app import build
from app.services import platform


def test_a_frozen_build_reports_the_list_its_freeze_wrote(tmp_path, monkeypatch):
    shipped = tmp_path / "frozen-packages.json"
    shipped.write_text(json.dumps([{"name": "SQLAlchemy", "version": "2.1.6"}, {"name": "alembic", "version": "1.20.1"}]))
    monkeypatch.setattr(platform, "FROZEN_PACKAGES", shipped)
    monkeypatch.setattr(build, "frozen", lambda: True)
    assert platform.packages() == [
        platform.Package(name="alembic", version="1.20.1"),
        platform.Package(name="SQLAlchemy", version="2.1.6"),
    ]


def test_a_frozen_build_without_the_list_falls_back_to_the_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "FROZEN_PACKAGES", tmp_path / "missing.json")
    monkeypatch.setattr(build, "frozen", lambda: True)
    names = {one.name.lower() for one in platform.packages()}
    assert "fastapi" in names


def test_a_broken_list_also_falls_back(tmp_path, monkeypatch):
    shipped = tmp_path / "frozen-packages.json"
    shipped.write_text('[{"name": "only-a-name"}]')
    monkeypatch.setattr(platform, "FROZEN_PACKAGES", shipped)
    monkeypatch.setattr(build, "frozen", lambda: True)
    assert "fastapi" in {one.name.lower() for one in platform.packages()}


def test_outside_the_desktop_app_the_list_file_is_not_read(tmp_path, monkeypatch):
    shipped = tmp_path / "frozen-packages.json"
    shipped.write_text(json.dumps([{"name": "only-this", "version": "1"}]))
    monkeypatch.setattr(platform, "FROZEN_PACKAGES", shipped)
    monkeypatch.setattr(build, "frozen", lambda: False)
    names = {one.name.lower() for one in platform.packages()}
    assert "only-this" not in names and "fastapi" in names


def test_the_list_lives_where_the_spec_bundles_it():
    # backend.spec puts it at "app/", i.e. beside app/build.json.
    assert build.STAMP.with_name("frozen-packages.json") == platform.FROZEN_PACKAGES
