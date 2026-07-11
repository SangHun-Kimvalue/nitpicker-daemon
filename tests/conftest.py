from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# bin/ 디렉터리를 패키지로 인식할 수 있도록 ROOT를 sys.path에 추가
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))