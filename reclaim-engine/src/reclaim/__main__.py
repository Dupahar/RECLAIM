"""Enable ``python -m reclaim <batch.json>``."""
import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
