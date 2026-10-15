"""pytest 根配置。

把项目根目录加入 sys.path，使 `from services.xxx import ...` 在测试中可用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
