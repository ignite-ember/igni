"""Entry point for `igni`, `ignite-ember` and `python -m ember_code`."""

from ember_code.cli import cli
from ember_code.core.dir_migration import migrate_home


def main():
    # Before anything reads the home directory. The rename is
    # idempotent and costs two stat calls once it has happened, and
    # ``paths.home_config_dir`` falls back to the legacy name anyway —
    # so a path that somehow skips this still works. Doing it here means
    # it happens once, early, rather than being something every reader
    # has to remember.
    migrate_home()
    cli()


if __name__ == "__main__":
    main()
