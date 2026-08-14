"""Make the package runnable: `python3 -m athena`.

Nothing but the entry point. cli.main() returns an exit status and this hands it
to the shell, so the CLI stays a function that can be called from a test with an
argv list instead of a process that can only be spawned.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
