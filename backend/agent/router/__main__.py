"""Entry point for: python -m backend.agent.router <command>"""

import sys
from backend.agent.router.cli import main

if __name__ == "__main__":
    sys.exit(main())
