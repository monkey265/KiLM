"""
Tests for the kilm relink command.
"""

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from kicad_lib_manager.commands.relink.command import (
    _shadowed,
    _table_entries,
    build_index,
    relink_text,
)
from kicad_lib_manager.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fixed_console_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")


def _sym_lib(*names: str) -> str:
    body = "".join(f'\t(symbol "{n}"\n\t)\n' for n in names)
    return f"(kicad_symbol_lib\n{body})\n"


@pytest.fixture
def lib_tree(tmp_path: Path) -> Path:
    """Managed library split into categories, plus a 3D model library."""
    lib = tmp_path / "lib"
    (lib / "symbols").mkdir(parents=True)
    (lib / "symbols" / "CAT_IC.kicad_sym").write_text(_sym_lib("ADC1", "Both"))
    (lib / "symbols" / "CAT_RF.kicad_sym").write_text(_sym_lib("AMP1", "Both"))
    for fp_lib, fps in {"CAT_QFN": ["QFN16"], "CAT_SOT": ["SOT23", "ADC1"]}.items():
        (lib / "footprints" / f"{fp_lib}.pretty").mkdir(parents=True)
        for fp in fps:
            (lib / "footprints" / f"{fp_lib}.pretty" / f"{fp}.kicad_mod").write_text(
                f'(footprint "{fp}")\n'
            )
    (lib / "models").mkdir()
    (lib / "models" / "QFN16.stp").write_text("STEP")
    return lib


@pytest.fixture
def index(lib_tree: Path):
    return build_index([lib_tree], {"KICAD_3D_CAT": lib_tree / "models"})


KNOWN_SYM = {"CAT_IC", "CAT_RF", "Device"}
KNOWN_FP = {"CAT_QFN", "CAT_SOT", "Resistor_SMD"}


def _relink(text: str, index, env: Optional[dict[str, str]] = None):
    return relink_text(text, index, KNOWN_SYM, KNOWN_FP, env or {})


def test_build_index_maps_items_to_libraries(index):
    assert index.symbols["ADC1"] == {"CAT_IC"}
    assert index.symbols["Both"] == {"CAT_IC", "CAT_RF"}
    assert index.footprints["QFN16"] == {"CAT_QFN"}
    assert index.models["QFN16.stp"] == "${KICAD_3D_CAT}"


def test_unconfigured_nickname_is_relinked(index):
    text = '(lib_id "OLD:ADC1")\n(symbol "OLD:ADC1"\n(property "Footprint" "OLD:QFN16")'
    r = _relink(text, index)

    assert '(lib_id "CAT_IC:ADC1")' in r.text
    assert '(symbol "CAT_IC:ADC1"' in r.text
    assert '"Footprint" "CAT_QFN:QFN16"' in r.text
    assert not r.unresolved


def test_symbols_and_footprints_are_looked_up_separately(index):
    # ADC1 is a symbol in CAT_IC and a footprint in CAT_SOT.
    r = _relink('(lib_id "OLD:ADC1")\n(footprint "OLD:ADC1"', index)

    assert '(lib_id "CAT_IC:ADC1")' in r.text
    assert '(footprint "CAT_SOT:ADC1"' in r.text


def test_configured_references_are_left_alone(index):
    text = '(lib_id "Device:ADC1")\n(footprint "Resistor_SMD:QFN16"\n(lib_id "CAT_IC:ADC1")'
    r = _relink(text, index)

    assert r.text == text
    assert not r.changes


def test_item_moved_out_of_managed_library_is_relinked(index):
    r = _relink('(lib_id "CAT_RF:ADC1")', index)

    assert r.text == '(lib_id "CAT_IC:ADC1")'


def test_ambiguous_and_missing_items_are_reported(index):
    text = '(lib_id "OLD:Both")\n(lib_id "OLD:Nope")'
    r = _relink(text, index)

    assert r.text == text
    assert any("OLD:Both (ambiguous: CAT_IC, CAT_RF)" in u for u in r.unresolved)
    assert any("OLD:Nope (not found)" in u for u in r.unresolved)


def test_sub_symbol_units_are_not_references(index):
    text = '(symbol "ADC1_0_1"'
    assert _relink(text, index).text == text


def test_unresolvable_model_path_is_relinked(index):
    r = _relink('(model "${UNDEFINED}/old/QFN16.stp"', index)

    assert r.text == '(model "${KICAD_3D_CAT}/QFN16.stp"'


def test_windows_absolute_model_path_is_relinked(index):
    r = _relink('(model "C:\\\\Users\\\\x\\\\QFN16.stp"', index)

    assert r.text == '(model "${KICAD_3D_CAT}/QFN16.stp"'


def test_resolving_and_builtin_model_paths_are_left_alone(index, lib_tree: Path):
    text = (
        '(model "${MINE}/QFN16.stp"\n'
        '(model "${KICAD10_3DMODEL_DIR}/Package_SO.3dshapes/SOIC-8.step"'
    )
    r = _relink(text, index, {"MINE": str(lib_tree / "models")})

    assert r.text == text


def test_missing_model_without_replacement_is_reported(index):
    r = _relink('(model "/gone/Other.stp"', index)

    assert r.unresolved == ["3D model /gone/Other.stp (not found)"]


def test_table_nicknames_follow_nested_tables(tmp_path: Path):
    nested = tmp_path / "system-fp-lib-table"
    nested.write_text(
        '(fp_lib_table\n\t(lib (name "Package_SO")(type "KiCad")(uri "/x")(options "")(descr ""))\n)\n'
    )
    table = tmp_path / "fp-lib-table"
    table.write_text(
        "(fp_lib_table\n"
        '\t(lib (name "KiCad") (type "Table") (uri "${SYS}/system-fp-lib-table") (options "") (descr ""))\n'
        '\t(lib (name "Mine")(type "KiCad")(uri "/y")(options "")(descr ""))\n'
        ")\n"
    )

    assert set(_table_entries(table, {"SYS": str(tmp_path)})) == {"Package_SO", "Mine"}


# ── CLI ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path, lib_tree: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (lib_tree / "models" / ".kilm_metadata").write_text('{"env_var": "KICAD_3D_CAT"}')
    config_mock = MagicMock()
    libs = [
        {"name": "lib", "path": str(lib_tree), "type": "github"},
        {"name": "models", "path": str(lib_tree / "models"), "type": "cloud"},
    ]
    config_mock.get_libraries.side_effect = lambda library_type=None: [
        lib for lib in libs if library_type in (None, lib["type"])
    ]
    monkeypatch.setattr(
        "kicad_lib_manager.commands.relink.command.Config", lambda: config_mock
    )

    kicad_config = tmp_path / "kicad"
    kicad_config.mkdir()
    (kicad_config / "sym-lib-table").write_text(
        '(sym_lib_table\n\t(lib (name "CAT_IC")(type "KiCad")(uri "/a")(options "")(descr ""))\n)\n'
    )
    (kicad_config / "fp-lib-table").write_text(
        '(fp_lib_table\n\t(lib (name "CAT_QFN")(type "KiCad")(uri "/b")(options "")(descr ""))\n)\n'
    )
    monkeypatch.setattr(
        "kicad_lib_manager.commands.relink.command.KiCadService.find_kicad_config_dir",
        staticmethod(lambda: kicad_config),
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.kicad_sch").write_text('(lib_id "OLD:ADC1")\n')
    return proj


def test_relink_cli_rewrites_and_backs_up(project: Path):
    result = runner.invoke(app, ["relink", str(project)])

    assert result.exit_code == 0, result.output
    assert "OLD:ADC1 -> CAT_IC:ADC1" in result.output
    assert (project / "board.kicad_sch").read_text() == '(lib_id "CAT_IC:ADC1")\n'
    assert list(project.glob("board.kicad_sch.backup.*"))


def test_relink_cli_dry_run_writes_nothing(project: Path):
    result = runner.invoke(app, ["relink", "--dry-run", str(project)])

    assert result.exit_code == 0, result.output
    assert "Would change 1 file(s)" in result.output
    assert (project / "board.kicad_sch").read_text() == '(lib_id "OLD:ADC1")\n'


def test_relink_cli_skips_file_open_in_kicad(project: Path):
    (project / "~board.kicad_sch.lck").write_text("{}")

    result = runner.invoke(app, ["relink", str(project)])

    assert "open in KiCad" in result.output
    assert (project / "board.kicad_sch").read_text() == '(lib_id "OLD:ADC1")\n'

    result = runner.invoke(app, ["relink", "--force", str(project)])
    assert (project / "board.kicad_sch").read_text() == '(lib_id "CAT_IC:ADC1")\n'


# ── Review fixes ──────────────────────────────────────────────────────────────


def test_table_nicknames_accept_unquoted_entries(tmp_path: Path):
    table = tmp_path / "sym-lib-table"
    table.write_text(
        "(sym_lib_table\n"
        '  (lib (name Device)(type Legacy)(uri ${KICAD_SYMBOL_DIR}/Device.lib)(options "")(descr ""))\n'
        '  (lib (name "Quoted")(type "KiCad")(uri "/q")(options "")(descr ""))\n'
        ")\n"
    )

    assert set(_table_entries(table, {})) == {"Device", "Quoted"}


def test_index_only_uses_registered_dirs(lib_tree: Path):
    stray = lib_tree / "examples" / "demo"
    stray.mkdir(parents=True)
    (stray / "STRAY.kicad_sym").write_text(_sym_lib("ADC1"))
    (stray / "STRAY.pretty").mkdir()

    index = build_index([lib_tree], {})

    assert index.symbols["ADC1"] == {"CAT_IC"}
    assert "STRAY" not in index.managed_footprint_libs


def test_missing_model_dir_is_skipped(lib_tree: Path):
    index = build_index([lib_tree], {"GONE": lib_tree / "nope"})

    assert index.models == {}


def test_project_relative_model_path_resolves(index, tmp_path: Path):
    (tmp_path / "3d").mkdir()
    (tmp_path / "3d" / "QFN16.stp").write_text("STEP")
    text = '(model "3d/QFN16.stp"'

    r = _relink(text, index, {"KIPRJMOD": str(tmp_path)})

    assert r.text == text


def test_project_table_shadowing_managed_nickname_is_foreign(index):
    # The project redefines CAT_RF to its own library, so CAT_RF:ADC1 is not
    # a managed reference that moved and must stay.
    text = '(lib_id "CAT_RF:ADC1")'
    r = relink_text(text, index, KNOWN_SYM, KNOWN_FP, {}, shadowed_sym={"CAT_RF"})

    assert r.text == text


def test_cached_symbol_not_duplicated(index):
    text = (
        '(lib_symbols\n(symbol "CAT_IC:ADC1"\n)\n(symbol "OLD:ADC1"\n)\n)\n'
        '(lib_id "OLD:ADC1")'
    )
    r = _relink(text, index)

    assert r.text.count('(symbol "CAT_IC:ADC1"') == 1
    assert '(lib_id "CAT_IC:ADC1")' in r.text


def test_kicad_env_vars_null_is_empty(tmp_path: Path):
    from kicad_lib_manager.services.kicad_service import KiCadService

    (tmp_path / "kicad_common.json").write_text('{"environment": {"vars": null}}')

    assert KiCadService().get_environment_variables(tmp_path) == {}


def test_relink_cli_refuses_without_global_tables(project: Path, tmp_path: Path):
    (tmp_path / "kicad" / "fp-lib-table").unlink()

    result = runner.invoke(app, ["relink", str(project)])

    assert result.exit_code == 1
    assert "global library tables" in result.output
    assert (project / "board.kicad_sch").read_text() == '(lib_id "OLD:ADC1")\n'


# ── Review round 2 ────────────────────────────────────────────────────────────


def test_same_named_symbol_and_footprint_libs_are_not_shadowed(
    lib_tree: Path, tmp_path: Path
):
    # CAT_IC exists as both a symbol library and a footprint library.
    (lib_tree / "footprints" / "CAT_IC.pretty").mkdir()
    index = build_index([lib_tree], {})
    sym_entry = {"CAT_IC": str(lib_tree / "symbols" / "CAT_IC.kicad_sym")}
    fp_entry = {"CAT_IC": str(lib_tree / "footprints" / "CAT_IC.pretty")}

    assert _shadowed(sym_entry, index.managed_symbol_paths, tmp_path) == set()
    assert _shadowed(fp_entry, index.managed_footprint_paths, tmp_path) == set()


def test_footprint_shadow_does_not_disable_symbol_checks(index):
    r = relink_text(
        '(lib_id "CAT_RF:ADC1")', index, KNOWN_SYM, KNOWN_FP, {}, shadowed_fp={"CAT_RF"}
    )

    assert r.text == '(lib_id "CAT_IC:ADC1")'


def test_two_broken_cache_entries_are_not_renamed_to_one(index):
    text = '(lib_symbols\n(symbol "OldA:ADC1"\n)\n(symbol "OldB:ADC1"\n)\n)'
    r = _relink(text, index)

    assert r.text.count('(symbol "CAT_IC:ADC1"') == 1


def test_shadowed_handles_unknown_vars_and_relative_uris(lib_tree: Path):
    index = build_index([lib_tree], {})
    paths = index.managed_symbol_paths
    rel = Path("..") / lib_tree.name / "symbols" / "CAT_IC.kicad_sym"
    project_dir = lib_tree.parent / "proj"

    assert _shadowed({"CAT_IC": "${NOPE}/CAT_IC.kicad_sym"}, paths, project_dir) == {
        "CAT_IC"
    }
    assert _shadowed({"CAT_IC": str(rel)}, paths, project_dir) == set()


def test_unresolved_nested_table_is_reported(tmp_path: Path):
    table = tmp_path / "sym-lib-table"
    table.write_text(
        "(sym_lib_table\n"
        '\t(lib (name "KiCad")(type "Table")(uri "${UNSET}/sym-lib-table")(options "")(descr ""))\n'
        ")\n"
    )
    missing: list[str] = []

    assert _table_entries(table, {}, missing=missing) == {}
    assert missing == ["${UNSET}/sym-lib-table"]


def test_missing_nested_global_table_degrades_instead_of_failing(
    project: Path, tmp_path: Path
):
    table = tmp_path / "kicad" / "sym-lib-table"
    table.write_text(
        table.read_text().replace(
            "(sym_lib_table\n",
            '(sym_lib_table\n\t(lib (name "KiCad")(type "Table")'
            '(uri "${KICAD9_TEMPLATE_DIR}/sym-lib-table")(options "")(descr ""))\n',
        )
    )
    (project / "board.kicad_sch").write_text(
        '(lib_id "OLD:ADC1")\n(lib_id "CAT_RF:ADC1")\n'
    )

    result = runner.invoke(app, ["relink", str(project)])

    assert result.exit_code == 0, result.output
    assert "export KICAD9_TEMPLATE_DIR=..." in result.output
    assert "OLD:ADC1 (library tables incomplete, not rewritten)" in result.output
    # A part that left a managed library is still repaired.
    assert (project / "board.kicad_sch").read_text() == (
        '(lib_id "OLD:ADC1")\n(lib_id "CAT_IC:ADC1")\n'
    )


def test_missing_nested_project_table_is_reported(project: Path):
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n\t(lib (name "Vendor")(type "Table")'
        '(uri "${VENDOR_DIR}/sym-lib-table")(options "")(descr ""))\n)\n'
    )

    result = runner.invoke(app, ["relink", str(project)])

    assert "Library tables not found: ${VENDOR_DIR}/sym-lib-table" in result.output
    assert (project / "board.kicad_sch").read_text() == '(lib_id "OLD:ADC1")\n'
