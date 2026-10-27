"""Run from backend root: python -m pymicroservices.app.migrate [upgrade|current|check]."""
import argparse
from pathlib import Path
from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['upgrade', 'current', 'check'], default='upgrade', nargs='?')
    args = parser.parse_args()
    config = Config(str(Path(__file__).with_name('alembic.ini')))
    if args.action == 'upgrade':
        command.upgrade(config, 'head')
    elif args.action == 'check':
        command.check(config)
    else:
        command.current(config, verbose=True)


if __name__ == '__main__':
    main()
