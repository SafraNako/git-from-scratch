"""``python -m kendi_git`` runs the ``kgit`` command line."""
import sys

from .cli import main

sys.exit(main())
