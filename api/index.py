"""Vercel 서버리스 진입점.

프로젝트 루트의 Flask 앱을 그대로 재사용한다. 루트가 sys.path에 없을 수 있어
직접 추가한 뒤 import 한다.
"""

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import app  # noqa: E402

__all__ = ["app"]
