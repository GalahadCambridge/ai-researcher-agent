"""Run repository-only literature evaluation after installing the project dependencies."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.literature.evaluation import main

    main()
