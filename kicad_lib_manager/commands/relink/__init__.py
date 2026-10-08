import typer

from .command import relink

relink_app = typer.Typer(
    name="relink",
    help="Repoint broken library references in KiCad projects to managed libraries",
    rich_markup_mode="rich",
    callback=relink,
    invoke_without_command=True,
)

__all__ = ["relink", "relink_app"]
