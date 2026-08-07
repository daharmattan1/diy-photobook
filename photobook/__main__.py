"""Enable `python -m photobook ...` as an alias for the `photobook` CLI."""

from .cli import cli

if __name__ == "__main__":
    cli()
