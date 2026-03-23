from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from keysight_scope_app.ui import main


if __name__ == "__main__":
    main()

