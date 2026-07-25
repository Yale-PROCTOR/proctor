import sys
from pathlib import Path

_LOCAL_TRANSFORMATION = Path(__file__).parents[1] / "stages" / "local-transformation"
sys.path.insert(0, str(_LOCAL_TRANSFORMATION))
