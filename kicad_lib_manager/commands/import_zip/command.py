"""
Import command: unpack a SamacSys/Mouser/UltraLibrarian/SnapMagic KiCad ZIP into the configured library.
"""

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from ...services.config_service import Config, LibraryDict
from ...utils.kicad_sexpr import extract_symbol_blocks, symbol_name
from ...utils.metadata import read_cloud_metadata, read_github_metadata

console = Console()

# ── Symbol helpers ────────────────────────────────────────────────────────────


def _fix_footprint_ref(
    block: str, lib_name: str, fp_libs: Optional[dict[str, str]] = None
) -> str:
    """Point the symbol's Footprint field at a library.

    fp_libs maps footprint names from the same ZIP to the library that holds
    them (the target, or the library where an existing copy was found); those
    win over any vendor prefix. Other bare names get lib_name; other prefixed
    names are left alone.
    """

    def _replace(m: re.Match[str]) -> str:
        val = m.group(1)
        name = val.split(":", 1)[1] if ":" in val else val
        if fp_libs and name in fp_libs:
            val = f"{fp_libs[name]}:{name}"
        elif ":" not in val:
            val = f"{lib_name}:{val}"
        return f'"Footprint" "{val}"'

    return re.sub(r'"Footprint" "([^"]+)"', _replace, block)


def _merge_symbols(
    src_file: Path,
    sym_lib: Path,
    lib_name: str,
    dry_run: bool,
    elsewhere: frozenset[str] = frozenset(),
    fp_libs: Optional[dict[str, str]] = None,
) -> tuple[list[str], list[str]]:
    """Append new symbols to sym_lib; skip names already in it or in elsewhere."""
    src_text = src_file.read_text(encoding="utf-8")
    dest_text = sym_lib.read_text(encoding="utf-8")
    existing = {symbol_name(b) for b in extract_symbol_blocks(dest_text)} | elsewhere

    added: list[str] = []
    skipped: list[str] = []
    new_blocks: list[str] = []

    for block in extract_symbol_blocks(src_text):
        name = symbol_name(block)
        if name in existing:
            skipped.append(name)
            continue
        block = _fix_footprint_ref(block, lib_name, fp_libs)
        new_blocks.append(block)
        added.append(name)

    if new_blocks and not dry_run:
        insert = "\n".join(new_blocks) + "\n"
        last = dest_text.rfind(")")
        sym_lib.write_text(
            dest_text[:last] + insert + dest_text[last:], encoding="utf-8"
        )

    return added, skipped


# ── Footprint helpers ─────────────────────────────────────────────────────────


_MODEL_PATH_RE = re.compile(
    # Longer/compound extensions must come before their literal prefixes
    # (e.g. "step.gz" before "step"), or the alternation matches the
    # shorter one first and leaves the rest of the extension dangling.
    r'\(model\s+(?:"([^"]+)"|(\S+\.(?:step\.gz|stp\.gz|step|stp)))'
)


def _fix_3d_path(text: str, model_prefix: str) -> str:
    """Point every model path at model_prefix, e.g. "${KICAD_3D_MYLIB}"."""

    def _replace(m: re.Match[str]) -> str:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        filename = Path(raw).name
        return f'(model "{model_prefix}/{filename}"'

    return _MODEL_PATH_RE.sub(_replace, text)


def _upgrade_fp(path: Path, kicad_cli: Optional[Path]) -> None:
    """Upgrade legacy (module ...) footprint format in-place using kicad-cli."""
    if kicad_cli is None or not kicad_cli.exists():
        return
    first_line = path.read_text(encoding="utf-8", errors="replace").lstrip()[:20]
    if not first_line.startswith("(module"):
        return

    with tempfile.TemporaryDirectory(prefix="kilm_fp_upgrade_") as tmp:
        in_pretty = Path(tmp) / "in.pretty"
        in_pretty.mkdir()
        shutil.copy2(path, in_pretty / path.name)
        out_dir = Path(tmp) / "out"

        cmd = _build_kicad_cli_cmd(
            kicad_cli,
            "fp",
            "upgrade",
            "--force",
            "--output",
            str(out_dir),
            str(in_pretty),
        )

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        except subprocess.TimeoutExpired:
            console.print(
                f"[yellow]  warn: kicad-cli fp upgrade timed out for {path.name}, skipping[/yellow]"
            )
            return
        except subprocess.CalledProcessError as exc:
            console.print(
                f"[yellow]  warn: kicad-cli fp upgrade failed for {path.name}: {exc.stderr.strip()}[/yellow]"
            )
            return
        upgraded = out_dir / path.name
        if upgraded.exists():
            shutil.copy2(upgraded, path)


def _upgrade_sym(sym_file: Path, kicad_cli: Optional[Path]) -> None:
    """Upgrade symbol file format in-place using kicad-cli."""
    if kicad_cli is None or not kicad_cli.exists():
        return
    cmd = _build_kicad_cli_cmd(kicad_cli, "sym", "upgrade", "--force", str(sym_file))
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.TimeoutExpired:
        console.print(
            f"[yellow]  warn: kicad-cli sym upgrade timed out for {sym_file.name}, skipping[/yellow]"
        )
    except subprocess.CalledProcessError as exc:
        console.print(
            f"[yellow]  warn: kicad-cli sym upgrade failed for {sym_file.name}: {exc.stderr.strip()}[/yellow]"
        )


def _build_kicad_cli_cmd(kicad_cli: Path, *args: str) -> list[str]:
    """Return the command list for kicad-cli, inserting the subcommand for AppImages."""
    if kicad_cli.suffix.lower() == ".appimage":
        return [str(kicad_cli), "kicad-cli", *args]
    return [str(kicad_cli), *args]


# ── ZIP extraction ───────────────────────────────────────────────────────────


def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract ZIP, rejecting any member whose resolved path escapes dest.

    Defense-in-depth against zip-slip: checks every member before extracting
    regardless of Python version or platform.
    """
    dest_resolved = dest.resolve()
    for member in zf.namelist():
        member_path = (dest / member).resolve()
        if member_path != dest_resolved and not member_path.is_relative_to(
            dest_resolved
        ):
            raise ValueError(f"Unsafe ZIP entry rejected: {member!r}")
    zf.extractall(dest)


# ── Per-ZIP import ────────────────────────────────────────────────────────────


def _symbol_names(sym_libs: list[Path]) -> set[str]:
    names: set[str] = set()
    for lib in sym_libs:
        names |= {
            symbol_name(b)
            for b in extract_symbol_blocks(lib.read_text(encoding="utf-8"))
        }
    return names


def _import_zip(
    zip_path: Path,
    sym_lib: Path,
    fp_dir: Path,
    models_dir: Path,
    model_prefix: str,
    kicad_cli: Optional[Path],
    dry_run: bool,
    known_symbols: Optional[set[str]] = None,
    all_fp_dirs: Optional[list[Path]] = None,
    run_fps: Optional[dict[str, str]] = None,
    result: Optional[dict[str, list[str]]] = None,
) -> dict[str, list[str]]:
    """Import one ZIP into sym_lib / fp_dir.

    Symbols named in known_symbols and footprints present in any of
    all_fp_dirs (the other libraries of a split library) are skipped rather
    than duplicated. known_symbols and run_fps (footprint name -> library
    added earlier in this run) are updated in place, so later files and ZIPs
    see this one's parts even in a dry run. Progress is recorded in result
    as it happens, so a caller still knows what was written if this raises.
    """
    known_symbols = known_symbols if known_symbols is not None else set()
    run_fps = run_fps if run_fps is not None else {}
    fp_dirs = [fp_dir] + [d for d in all_fp_dirs or [] if d != fp_dir]
    fp_libs: dict[str, str] = {}
    if result is None:
        result = {}
    for key in ("sym", "sym_skipped", "fp", "models"):
        result.setdefault(key, [])

    with tempfile.TemporaryDirectory(prefix="kilm_import_") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, tmp_path)

        # Vendor ZIPs disagree on directory layout (SamacSys/Mouser use
        # "KiCad"/"3D" dirs, UltraLibrarian uses "KiCADv6" with a nested
        # "*.pretty" dir, SnapMagic has no wrapping dir at all) so search
        # the whole extracted tree by extension instead of by dir name.

        # 3D models
        for f in tmp_path.rglob("*"):
            if f.suffix.lower() in (".stp", ".step") or f.name.lower().endswith(
                (".stp.gz", ".step.gz")
            ):
                dest = models_dir / f.name
                if dest.exists() or f.name in result["models"]:
                    console.print(f"  3D   skip (exists): {f.name}")
                else:
                    console.print(f"  3D   add: {f.name}")
                    if not dry_run:
                        models_dir.mkdir(exist_ok=True)
                        shutil.copy2(f, dest)
                    result["models"].append(f.name)

        # Footprints
        for f in tmp_path.rglob("*.kicad_mod"):
            dest = fp_dir / f.name
            existing_in = next((d for d in fp_dirs if (d / f.name).exists()), None)
            if existing_in is not None:
                where = "" if existing_in == fp_dir else f" in {existing_in.stem}"
                console.print(f"  FP   skip (exists{where}): {f.name}")
                fp_libs[f.stem] = existing_in.stem
                continue
            if f.stem in run_fps:
                console.print(f"  FP   skip (added earlier in this run): {f.name}")
                fp_libs[f.stem] = run_fps[f.stem]
                continue
            console.print(f"  FP   add: {f.name}")
            fp_libs[f.stem] = fp_dir.stem
            run_fps[f.stem] = fp_dir.stem
            if not dry_run:
                text = _fix_3d_path(f.read_text(encoding="utf-8"), model_prefix)
                f.write_text(text, encoding="utf-8")
                _upgrade_fp(f, kicad_cli)
                fp_dir.mkdir(exist_ok=True)
                shutil.copy2(f, dest)
            result["fp"].append(f.name)

        # Symbols
        for f in tmp_path.rglob("*.kicad_sym"):
            if not dry_run:
                _upgrade_sym(f, kicad_cli)
            added, skipped = _merge_symbols(
                f, sym_lib, fp_dir.stem, dry_run, frozenset(known_symbols), fp_libs
            )
            known_symbols.update(added)
            for name in added:
                console.print(f"  SYM  add: {name}")
            for name in skipped:
                console.print(f"  SYM  skip (exists): {name}")
            result["sym"].extend(added)
            result["sym_skipped"].extend(skipped)

    return result


# ── Command ───────────────────────────────────────────────────────────────────

_KICAD_CLI_CANDIDATES: tuple[Path, ...] = (
    Path.home() / "AppImages" / "kicad.appimage",
    Path("/usr/bin/kicad-cli"),
    Path("/usr/local/bin/kicad-cli"),
    Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
)


def _detect_kicad_cli() -> Optional[Path]:
    """Return kicad-cli path if found on PATH or a location in _KICAD_CLI_CANDIDATES."""
    on_path = shutil.which("kicad-cli")
    if on_path is not None:
        return Path(on_path)
    for candidate in _KICAD_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


_PART_INFO_FILE = "part_info.txt"


def _read_part_info(zip_path: Path) -> dict[str, str]:
    """Key=value pairs of a SamacSys part_info.txt; empty if the ZIP has none."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if Path(member).name == _PART_INFO_FILE:
                text = zf.read(member).decode("utf-8", errors="replace")
                pairs = (line.split("=", 1) for line in text.splitlines())
                return {
                    k.strip(): v.strip() for k, v in (p for p in pairs if len(p) == 2)
                }
    return {}


def _category_map(lib_path: Path, section: str) -> dict[str, str]:
    """import_categories.<section> from the library's kilm.yaml.

    Maps a vendor category (SamacSys PartCategory for "symbols",
    PackageCategory for "footprints") to a library name.
    """
    metadata = read_github_metadata(lib_path) or {}
    categories = metadata.get("import_categories")
    mapping = categories.get(section) if isinstance(categories, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items()}


def _check_lib_name(candidates: list[Path], name: Optional[str], kind: str) -> None:
    if name is not None and name not in {c.stem for c in candidates}:
        available = ", ".join(c.stem for c in candidates)
        console.print(f"[red]No {kind} library '{name}'. Available: {available}[/red]")
        raise typer.Exit(1)


def _choose_lib(
    candidates: list[Path],
    explicit: Optional[str],
    mapping: dict[str, str],
    category: Optional[str],
    kind: str,
    option: str,
) -> Optional[Path]:
    """Pick the target library: explicit option, then kilm.yaml category, then the only one.

    Prints why and returns None when no library can be chosen.
    """
    by_name = {c.stem: c for c in candidates}
    if explicit is not None:
        return by_name[explicit]
    if category and category in mapping:
        target = mapping[category]
        if target in by_name:
            console.print(f"[dim]  {kind}: '{category}' -> {target} (kilm.yaml)[/dim]")
            return by_name[target]
        console.print(
            f"[red]  kilm.yaml maps {kind} category '{category}' to '{target}', "
            f"which does not exist[/red]"
        )
        return None
    if len(candidates) == 1:
        return candidates[0]
    hint = (
        f" (category '{category}' has no entry in kilm.yaml import_categories)"
        if category
        else ""
    )
    console.print(
        f"[red]  Several {kind} libraries found, choose one with {option}{hint}: "
        f"{', '.join(by_name)}[/red]"
    )
    return None


def _resolve_models(
    lib_path: Path, default_name: str, cloud_libs: list[LibraryDict]
) -> tuple[Path, str]:
    """Return (models dir, model path prefix) for the whole library.

    A 3D library registered with 'kilm add-3d' inside lib_path is used via its
    environment variable. Otherwise models go into the library's existing
    *.3dshapes dir (one named after the library, or the only one; else
    <default_name>.3dshapes), referenced through the
    library's own environment variable from kilm.yaml, which 'kilm setup'
    defines in KiCad; without one, the absolute path is used.
    """
    for lib in cloud_libs:
        models_dir = Path(lib["path"])
        if not models_dir.is_relative_to(lib_path):
            continue
        metadata = read_cloud_metadata(models_dir) or {}
        env_var = metadata.get("env_var")
        if isinstance(env_var, str) and env_var:
            return models_dir, f"${{{env_var}}}"
    metadata = read_github_metadata(lib_path) or {}
    existing = sorted(d for d in lib_path.glob("*.3dshapes") if d.is_dir())
    preferred = {default_name, str(metadata.get("name", "")), lib_path.name}
    named = [d for d in existing if d.stem in preferred]
    if named:
        models_dir = named[0]
    elif len(existing) == 1:
        models_dir = existing[0]
    else:
        if existing:
            console.print(
                f"[yellow]Several *.3dshapes dirs in {lib_path}; register the right "
                f"one with 'kilm add-3d'. Using {default_name}.3dshapes[/yellow]"
            )
        models_dir = lib_path / f"{default_name}.3dshapes"
    lib_env_var = metadata.get("env_var")
    if isinstance(lib_env_var, str) and lib_env_var:
        return models_dir, f"${{{lib_env_var}}}/{models_dir.name}"
    return models_dir, models_dir.resolve().as_posix()


def import_zip(
    zip_files: Annotated[
        list[Path],
        typer.Argument(
            help="SamacSys/Mouser/UltraLibrarian/SnapMagic ZIP file(s) to import"
        ),
    ],
    library: Annotated[
        Optional[str],
        typer.Option(
            "--library",
            "-l",
            help="Target library name (default: first github library)",
        ),
    ] = None,
    symbol_lib: Annotated[
        Optional[str],
        typer.Option(
            "--symbol-lib",
            "-s",
            help="Symbol library to add to, e.g. MyLib_IC (required if there are several)",
        ),
    ] = None,
    footprint_lib: Annotated[
        Optional[str],
        typer.Option(
            "--footprint-lib",
            "-f",
            help="Footprint library to add to, e.g. MyLib_QFN (required if there are several)",
        ),
    ] = None,
    kicad_cli_path: Annotated[
        Optional[Path],
        typer.Option(
            "--kicad-cli", help="Path to kicad-cli or kicad.appimage for format upgrade"
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show what would be imported without making changes"
        ),
    ] = False,
) -> None:
    """Import SamacSys/Mouser/UltraLibrarian/SnapMagic KiCad ZIP(s) into the configured library.

    Each ZIP should be a standard SamacSys multi-EDA archive (as downloaded
    from Mouser or component search) or an UltraLibrarian KiCad export.
    The command extracts the KiCad files
    and merges them into the library. Run 'kilm setup' afterwards to register
    any newly added libraries in KiCad.
    """
    config = Config()
    github_libs = config.get_libraries(library_type="github")
    if not github_libs:
        console.print("[red]No github library configured. Run 'kilm init' first.[/red]")
        raise typer.Exit(1)

    # Resolve target library
    target_lib = None
    for lib in github_libs:
        if library is None or lib.get("name") == library:
            target_lib = lib
            break

    if target_lib is None:
        console.print(f"[red]Library '{library}' not found in config.[/red]")
        raise typer.Exit(1)

    lib_path = Path(target_lib["path"])
    if not lib_path.exists():
        console.print(f"[red]Library path does not exist: {lib_path}[/red]")
        raise typer.Exit(1)

    # Find symbol lib and footprint dir
    sym_candidates = (
        sorted((lib_path / "symbols").glob("*.kicad_sym"))
        if (lib_path / "symbols").exists()
        else []
    )
    fp_candidates = (
        sorted((lib_path / "footprints").glob("*.pretty"))
        if (lib_path / "footprints").exists()
        else []
    )

    if not sym_candidates:
        console.print(f"[red]No .kicad_sym file found under {lib_path}/symbols/[/red]")
        raise typer.Exit(1)
    if not fp_candidates:
        console.print(
            f"[red]No .pretty directory found under {lib_path}/footprints/[/red]"
        )
        raise typer.Exit(1)

    _check_lib_name(sym_candidates, symbol_lib, "symbol")
    _check_lib_name(fp_candidates, footprint_lib, "footprint")
    sym_map = _category_map(lib_path, "symbols")
    fp_map = _category_map(lib_path, "footprints")
    default_models_name = (
        sym_candidates[0].stem if len(sym_candidates) == 1 else lib_path.name
    )
    models_dir, model_prefix = _resolve_models(
        lib_path, default_models_name, config.get_libraries(library_type="cloud")
    )
    if not model_prefix.startswith("${"):
        console.print(
            "[yellow]3D model paths will be absolute to this machine. Set env_var "
            "in kilm.yaml (kilm init) or register a 3D library (kilm add-3d) to "
            "share the library.[/yellow]"
        )
    known_symbols = _symbol_names(sym_candidates)
    run_fps: dict[str, str] = {}

    # Resolve kicad-cli
    if kicad_cli_path is not None and not kicad_cli_path.exists():
        console.print(f"[red]kicad-cli not found at: {kicad_cli_path}[/red]")
        raise typer.Exit(1)
    kicad_cli = kicad_cli_path if kicad_cli_path else _detect_kicad_cli()
    if kicad_cli:
        console.print(f"[dim]kicad-cli: {kicad_cli}[/dim]")
    else:
        console.print("[dim]kicad-cli not found - format upgrade skipped[/dim]")

    if dry_run:
        console.print("[yellow]Dry run - no changes will be made[/yellow]")

    totals: dict[str, list[str]] = {"sym": [], "fp": [], "models": []}
    unplaced: list[str] = []
    failed: list[str] = []

    for zip_path in zip_files:
        zip_path = zip_path.expanduser().resolve()
        if not zip_path.exists():
            console.print(f"[yellow]Skipping {zip_path.name}: file not found[/yellow]")
            continue
        if not zipfile.is_zipfile(zip_path):
            console.print(f"[yellow]Skipping {zip_path.name}: not a valid ZIP[/yellow]")
            continue

        console.print(f"\n[cyan]Importing {zip_path.name}[/cyan]")
        try:
            info = _read_part_info(zip_path)
        except Exception as exc:
            console.print(f"[red]  error: {zip_path.name}: {exc}[/red]")
            failed.append(zip_path.name)
            continue
        sym_lib = _choose_lib(
            sym_candidates,
            symbol_lib,
            sym_map,
            info.get("PartCategory"),
            "symbol",
            "--symbol-lib",
        )
        fp_dir = _choose_lib(
            fp_candidates,
            footprint_lib,
            fp_map,
            info.get("PackageCategory"),
            "footprint",
            "--footprint-lib",
        )
        if sym_lib is None or fp_dir is None:
            unplaced.append(zip_path.name)
            continue
        console.print(
            f"[dim]  Target: {sym_lib.stem} / {fp_dir.stem} / {model_prefix}[/dim]"
        )
        r: dict[str, list[str]] = {}
        try:
            _import_zip(
                zip_path,
                sym_lib,
                fp_dir,
                models_dir,
                model_prefix,
                kicad_cli,
                dry_run,
                known_symbols,
                fp_candidates,
                run_fps,
                r,
            )
        except Exception as exc:
            console.print(f"[red]  error: {zip_path.name}: {exc}[/red]")
            written = [f"{len(r.get(k, []))} {k}" for k in ("sym", "fp", "models")]
            if any(r.get(k) for k in ("sym", "fp", "models")):
                console.print(
                    f"[yellow]  partially imported before the error: "
                    f"{', '.join(written)}[/yellow]"
                )
            failed.append(zip_path.name)
        totals["sym"].extend(r.get("sym", []))
        totals["fp"].extend(r.get("fp", []))
        totals["models"].extend(r.get("models", []))

    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  Symbols   added: {len(totals['sym'])}")
    console.print(f"  Footprints added: {len(totals['fp'])}")
    console.print(f"  3D models  added: {len(totals['models'])}")

    if any(totals.values()) and not dry_run:
        console.print("\n[green]Import complete.[/green]")
        console.print(
            "[dim]Note: If this library is not yet configured in KiCad, run 'kilm setup' to register it.[/dim]"
        )
    elif dry_run:
        console.print(
            "\n[dim]Dry run complete - run without --dry-run to apply changes.[/dim]"
        )
    else:
        console.print("\n[dim]Nothing new added.[/dim]")

    if unplaced:
        console.print(
            f"[red]Not imported (no target library): {', '.join(unplaced)}[/red]"
        )
    if failed:
        console.print(f"[red]Failed: {', '.join(failed)}[/red]")
    if unplaced or failed:
        raise typer.Exit(1)
