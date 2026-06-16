import sys
from pathlib import Path

# Make the repository root importable so tests can do `from artifact_store import ...`
# and `from configs.config import ...` regardless of pytest's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
