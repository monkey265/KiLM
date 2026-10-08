"""
Tests for locating a KiCad installation via kicad-cli.
"""

from pathlib import Path

import pytest

from kicad_lib_manager.utils import kicad_install


def _fake_install(root: Path, cli_rel: str, share_rel: str) -> Path:
    cli = root / cli_rel
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    (root / share_rel / "template").mkdir(parents=True)
    return cli


@pytest.mark.parametrize(
    ("cli_rel", "share_rel"),
    [
        ("usr/bin/kicad-cli", "usr/share/kicad"),
        ("KiCad/9.0/bin/kicad-cli.exe", "KiCad/9.0/share/kicad"),
        ("KiCad.app/Contents/MacOS/kicad-cli", "KiCad.app/Contents/SharedSupport"),
    ],
)
def test_share_dir_found_next_to_kicad_cli(
    tmp_path: Path, cli_rel: str, share_rel: str
):
    cli = _fake_install(tmp_path, cli_rel, share_rel)

    assert kicad_install.kicad_share_dir(cli) == tmp_path / share_rel


def test_install_vars_cover_installed_and_older_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cli = _fake_install(tmp_path, "usr/bin/kicad-cli", "usr/share/kicad")
    monkeypatch.setattr(kicad_install, "kicad_major_version", lambda _: 9)

    install_vars = kicad_install.kicad_install_vars(cli)

    template = (tmp_path / "usr/share/kicad/template").as_posix()
    assert install_vars["KICAD9_TEMPLATE_DIR"] == template
    assert install_vars["KICAD8_TEMPLATE_DIR"] == template
    assert "KICAD10_TEMPLATE_DIR" not in install_vars


def test_install_vars_empty_without_kicad(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kicad_install, "detect_kicad_cli", lambda: None)

    assert kicad_install.kicad_install_vars() == {}


def test_install_vars_include_kicad5_names_and_skip_kicad5_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cli = _fake_install(tmp_path, "usr/bin/kicad-cli", "usr/share/kicad")
    monkeypatch.setattr(kicad_install, "kicad_major_version", lambda _: 9)

    install_vars = kicad_install.kicad_install_vars(cli)

    assert install_vars["KISYSMOD"].endswith("/footprints")
    assert install_vars["KISYS3DMOD"].endswith("/3dmodels")
    assert "KICAD5_TEMPLATE_DIR" not in install_vars


@pytest.mark.parametrize(("out", "major"), [("9.0.3\n", 9), ("9.99.0-123\n", 10)])
def test_major_version_maps_dev_builds_to_next_major(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out: str, major: int
):
    class _Done:
        stdout = out

    monkeypatch.setattr(kicad_install.subprocess, "run", lambda *a, **k: _Done())

    assert kicad_install.kicad_major_version(tmp_path / "kicad-cli") == major


def test_no_version_probe_without_share_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cli = tmp_path / "kicad.appimage"
    cli.write_text("")

    def _fail(_: Path) -> int:
        raise AssertionError("kicad-cli should not be launched")

    monkeypatch.setattr(kicad_install, "kicad_major_version", _fail)

    assert kicad_install.kicad_install_vars(cli) == {}
