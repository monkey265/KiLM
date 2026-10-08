"""
Locate a KiCad installation through its kicad-cli executable.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# Newest first, so the most recent installation wins when several exist.
_WINDOWS_VERSIONS = range(11, 6, -1)

KICAD_CLI_CANDIDATES: tuple[Path, ...] = (
    Path.home() / "AppImages" / "kicad.appimage",
    Path("/usr/bin/kicad-cli"),
    Path("/usr/local/bin/kicad-cli"),
    Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    *(
        Path(f"C:/Program Files/KiCad/{v}.0/bin/kicad-cli.exe")
        for v in _WINDOWS_VERSIONS
    ),
)

# Library dirs of an installation, by the suffix of the KICADn_* variable
# KiCad defines for them at runtime (not stored in kicad_common.json).
_INSTALL_SUBDIRS = {
    "SYMBOL_DIR": "symbols",
    "FOOTPRINT_DIR": "footprints",
    "3DMODEL_DIR": "3dmodels",
    "TEMPLATE_DIR": "template",
}
_OLDEST_VERSION = 5


def detect_kicad_cli() -> Optional[Path]:
    """Return kicad-cli from PATH or the first existing KICAD_CLI_CANDIDATES entry."""
    on_path = shutil.which("kicad-cli")
    if on_path is not None:
        return Path(on_path)
    for candidate in KICAD_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def build_kicad_cli_cmd(kicad_cli: Path, *args: str) -> list[str]:
    """Return the command list for kicad-cli, inserting the subcommand for AppImages."""
    if kicad_cli.suffix.lower() == ".appimage":
        return [str(kicad_cli), "kicad-cli", *args]
    return [str(kicad_cli), *args]


def kicad_share_dir(kicad_cli: Path) -> Optional[Path]:
    """The installation's share dir (holding template/, symbols/, ...), if found.

    Derived from where kicad-cli lives: <prefix>/bin -> <prefix>/share/kicad
    on Linux and Windows, Contents/MacOS -> Contents/SharedSupport on macOS.
    AppImages keep it inside the image, so they are not resolved.
    """
    real = kicad_cli.resolve()
    for share in (
        real.parent.parent / "share" / "kicad",
        real.parent.parent / "SharedSupport",
    ):
        if (share / "template").is_dir():
            return share
    return None


def kicad_major_version(kicad_cli: Path) -> Optional[int]:
    try:
        out = subprocess.run(
            build_kicad_cli_cmd(kicad_cli, "version"),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*(\d+)\.", out)
    return int(m.group(1)) if m else None


def kicad_install_vars(kicad_cli: Optional[Path] = None) -> dict[str, str]:
    """Values for KiCad's built-in KICADn_*_DIR variables, from the installation.

    Covers the installed major version and older ones (configs migrated from
    earlier versions keep their variable names). Empty if KiCad is not found.
    """
    kicad_cli = kicad_cli or detect_kicad_cli()
    if kicad_cli is None:
        return {}
    share = kicad_share_dir(kicad_cli)
    version = kicad_major_version(kicad_cli)
    if share is None or version is None:
        return {}
    install_vars: dict[str, str] = {}
    for suffix, subdir in _INSTALL_SUBDIRS.items():
        path = (share / subdir).as_posix()
        install_vars[f"KICAD_{suffix}"] = path
        for v in range(_OLDEST_VERSION, version + 1):
            install_vars[f"KICAD{v}_{suffix}"] = path
    return install_vars
