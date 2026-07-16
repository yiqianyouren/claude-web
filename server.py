"""Development compatibility entry point.

All server implementation lives in :mod:`claude_web.server`.  Keeping this
file as a thin launcher prevents the source checkout and installed package
from drifting into two different runtimes.
"""

from claude_web.server import app, main, print_extension_path

__all__ = ["app", "main", "print_extension_path"]


if __name__ == "__main__":
    main()
