"""让 `pytest` 在 connector/ 目录下直接找到 `course_generator` 包。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
