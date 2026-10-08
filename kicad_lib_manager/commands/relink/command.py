"""
Relink command: repoint broken library references in KiCad project files.

After a library is split, merged or renamed, schematics and PCBs keep
referencing the old "Nickname:Item" names. This command finds references
whose nickname is no longer configured (or whose item moved out of a managed
library), looks the item up in the managed libraries and rewrites the
reference when exactly one library provides it. Footprint 3D model paths that
no longer resolve are repointed to a managed 3D library that has the file.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ...services.config_service import Config
from ...services.kicad_service import KiCadService
from ...utils.backup import create_backup
from ...utils.metadata import read_cloud_metadata
from ..import_zip.command import _extract_symbol_blocks, _symbol_name

console = Console()

# "Nickname:Item" references, by kind. Group 1 is the text before the
# reference, group 2 the nickname and group 3 the item name.
_SYMBOL_REF_RE = re.compile(r'(\(lib_id "|\(symbol ")([^":]+):([^"]+)"')
_FOOTPRINT_REF_RE = re.compile(r'("Footprint" "|\(footprint ")([^":]+):([^"]+)"')
_MODEL_RE = re.compile(r'\(model "([^"]+)"')
_VAR_RE = re.compile(r"\$\{([^}]+)\}")
# KiCad's own versioned variables (KICAD9_3DMODEL_DIR, ...) are defined by the
# installation, not by kicad_common.json, so they are assumed to resolve.
_BUILTIN_VAR_RE = re.compile(r"KICAD\d+_")
_TABLE_ENTRY_RE = re.compile(
    r'\(lib\s+\(name\s+"([^"]+)"\)\s*\(type\s+"([^"]+)"\)\s*\(uri\s+"([^"]+)"\)'
)


@dataclass
class LibraryIndex:
    """Which managed libraries provide each symbol, footprint and 3D model."""

    symbols: dict[str, set[str]] = field(default_factory=dict)
    footprints: dict[str, set[str]] = field(default_factory=dict)
    managed_symbol_libs: dict[str, set[str]] = field(default_factory=dict)
    managed_footprint_libs: dict[str, set[str]] = field(default_factory=dict)
    # 3D model file name -> "${ENV_VAR}" prefix of the 3D library holding it
    models: dict[str, str] = field(default_factory=dict)


@dataclass
class RelinkResult:
    text: str
    changes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def build_index(lib_paths: list[Path], model_dirs: dict[str, Path]) -> LibraryIndex:
    """Index symbol and footprint libraries under lib_paths and model dirs.

    model_dirs maps an environment variable name to the 3D model directory it
    points at.
    """
    index = LibraryIndex()
    for lib_path in lib_paths:
        for sym_file in sorted(lib_path.rglob("*.kicad_sym")):
            text = sym_file.read_text(encoding="utf-8")
            names = {_symbol_name(b) for b in _extract_symbol_blocks(text)}
            index.managed_symbol_libs[sym_file.stem] = names
            for name in names:
                index.symbols.setdefault(name, set()).add(sym_file.stem)
        for fp_dir in sorted(lib_path.rglob("*.pretty")):
            names = {f.stem for f in fp_dir.glob("*.kicad_mod")}
            index.managed_footprint_libs[fp_dir.stem] = names
            for name in names:
                index.footprints.setdefault(name, set()).add(fp_dir.stem)
    for env_var, model_dir in sorted(model_dirs.items()):
        for model in sorted(model_dir.iterdir()):
            if model.is_file() and not model.name.startswith("."):
                index.models.setdefault(model.name, f"${{{env_var}}}")
    return index


def _relink_refs(
    result: RelinkResult,
    pattern: re.Pattern[str],
    kind: str,
    provided_by: dict[str, set[str]],
    managed: dict[str, set[str]],
    known_nicknames: set[str],
) -> None:
    def _replace(m: re.Match[str]) -> str:
        nick, item = m.group(2), m.group(3)
        if nick in managed:
            broken = item not in managed[nick]
        else:
            broken = nick not in known_nicknames
        if not broken:
            return m.group(0)
        candidates = sorted(provided_by.get(item, set()))
        if len(candidates) != 1:
            reason = (
                f"ambiguous: {', '.join(candidates)}" if candidates else "not found"
            )
            result.unresolved.append(f"{kind} {nick}:{item} ({reason})")
            return m.group(0)
        new_ref = f"{candidates[0]}:{item}"
        result.changes.append(f"{kind} {nick}:{item} -> {new_ref}")
        return f'{m.group(1)}{new_ref}"'

    result.text = pattern.sub(_replace, result.text)


def _expand(path: str, env: dict[str, str]) -> str:
    """Substitute ${VAR} from env, leaving unknown variables in place."""

    def _sub(m: re.Match[str]) -> str:
        return env.get(m.group(1), m.group(0))

    return _VAR_RE.sub(_sub, path)


def _model_resolves(path: str, env: dict[str, str]) -> bool:
    for var in _VAR_RE.findall(path):
        if _BUILTIN_VAR_RE.match(var):
            return True
        if var not in env:
            return False
    return Path(_expand(path, env)).is_file()


def relink_text(
    text: str,
    index: LibraryIndex,
    known_sym: set[str],
    known_fp: set[str],
    env: dict[str, str],
) -> RelinkResult:
    """Rewrite broken references in the text of a .kicad_sch or .kicad_pcb file.

    known_sym / known_fp are the nicknames configured in KiCad (global and
    project tables). env resolves ${VAR} in 3D model paths.
    """
    result = RelinkResult(text)
    _relink_refs(
        result,
        _SYMBOL_REF_RE,
        "symbol",
        index.symbols,
        index.managed_symbol_libs,
        known_sym,
    )
    _relink_refs(
        result,
        _FOOTPRINT_REF_RE,
        "footprint",
        index.footprints,
        index.managed_footprint_libs,
        known_fp,
    )

    def _replace_model(m: re.Match[str]) -> str:
        path = m.group(1)
        if _model_resolves(path, env):
            return m.group(0)
        name = Path(path.replace("\\", "/")).name
        prefix = index.models.get(name)
        if prefix is None:
            result.unresolved.append(f"3D model {path} (not found)")
            return m.group(0)
        new_path = f"{prefix}/{name}"
        result.changes.append(f"3D model {path} -> {new_path}")
        return f'(model "{new_path}"'

    result.text = _MODEL_RE.sub(_replace_model, result.text)
    return result


def _table_nicknames(
    table: Path, env: dict[str, str], seen: frozenset[Path] = frozenset()
) -> set[str]:
    """Nicknames in a lib table, following nested (type "Table") entries."""
    if not table.is_file() or table in seen:
        return set()
    names: set[str] = set()
    for name, lib_type, uri in _TABLE_ENTRY_RE.findall(
        table.read_text(encoding="utf-8")
    ):
        if lib_type == "Table":
            nested = Path(_expand(str(uri), env))
            names |= _table_nicknames(nested, env, seen | {table})
        else:
            names.add(name)
    return names


def _project_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob("*.kicad_sch")) + sorted(p.glob("*.kicad_pcb")))
        elif p.suffix in (".kicad_sch", ".kicad_pcb"):
            files.append(p)
        else:
            console.print(
                f"[yellow]Skipping {p}: not a project dir or KiCad file[/yellow]"
            )
    return files


def relink(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Project directories or .kicad_sch/.kicad_pcb files"),
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show changes without writing files")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Write files even if KiCad has them open"),
    ] = False,
) -> None:
    """Repoint broken symbol, footprint and 3D model references to managed libraries.

    A reference is rewritten only when its library is no longer configured in
    KiCad (or the item left a managed library) and exactly one managed library
    provides the item. Each changed file is backed up first.
    """
    config = Config()
    lib_paths = [
        Path(lib["path"]) for lib in config.get_libraries(library_type="github")
    ]
    if not lib_paths:
        console.print("[red]No github library configured. Run 'kilm init' first.[/red]")
        raise typer.Exit(1)

    model_dirs: dict[str, Path] = {}
    for lib in config.get_libraries(library_type="cloud"):
        metadata = read_cloud_metadata(Path(lib["path"])) or {}
        env_var = metadata.get("env_var")
        if isinstance(env_var, str) and env_var:
            model_dirs[env_var] = Path(lib["path"])

    kicad_config = KiCadService.find_kicad_config_dir()
    env = {**os.environ, **KiCadService().get_environment_variables(kicad_config)}
    global_sym = _table_nicknames(kicad_config / "sym-lib-table", env)
    global_fp = _table_nicknames(kicad_config / "fp-lib-table", env)

    index = build_index(lib_paths, model_dirs)
    files = _project_files(paths)
    if not files:
        console.print("[yellow]No .kicad_sch or .kicad_pcb files found.[/yellow]")
        raise typer.Exit(1)

    if dry_run:
        console.print("[yellow]Dry run - no files will be changed[/yellow]")

    changed_files = 0
    unresolved_total = 0
    for f in files:
        project_dir = f.parent
        project_env = {**env, "KIPRJMOD": str(project_dir)}
        known_sym = global_sym | _table_nicknames(
            project_dir / "sym-lib-table", project_env
        )
        known_fp = global_fp | _table_nicknames(
            project_dir / "fp-lib-table", project_env
        )
        result = relink_text(
            f.read_text(encoding="utf-8"), index, known_sym, known_fp, project_env
        )
        if not result.changes and not result.unresolved:
            continue

        console.print(f"\n[cyan]{f}[/cyan]")
        for change in sorted(set(result.changes)):
            console.print(f"  {change}")
        for item in sorted(set(result.unresolved)):
            console.print(f"  [yellow]unresolved: {item}[/yellow]")
        unresolved_total += len(set(result.unresolved))
        if not result.changes:
            continue

        lock = f.with_name(f"~{f.name}.lck")
        if lock.exists() and not force and not dry_run:
            console.print(
                f"  [red]skipped: open in KiCad ({lock.name}); close it or use --force[/red]"
            )
            continue
        changed_files += 1
        if not dry_run:
            backup = create_backup(f)
            f.write_text(result.text, encoding="utf-8")
            console.print(f"  [dim]backup: {backup.name}[/dim]")

    verb = "Would change" if dry_run else "Changed"
    console.print(f"\n[bold]{verb} {changed_files} file(s)[/bold]")
    if unresolved_total:
        console.print(
            f"[yellow]{unresolved_total} reference(s) left unresolved - fix them in KiCad[/yellow]"
        )
