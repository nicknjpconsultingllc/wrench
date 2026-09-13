"""Makes the test doubles in tests/fakes.py importable as ``fakes``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
