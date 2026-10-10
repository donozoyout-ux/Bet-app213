"""Progressive market odds backfill for target leagues; low resource consumption."""
from .cli import main

if __name__ == '__main__':
    main('market_backfill')
