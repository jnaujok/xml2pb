"""Allow ``python -m xml2pb``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
