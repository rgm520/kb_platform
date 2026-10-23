import sys
import os

# 让测试能 import kbplatform 与 kb
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


@pytest.fixture()
def conn(tmp_path):
    from kbplatform.db import init_db
    c = init_db(str(tmp_path / "kb.db"))
    yield c
    c.close()
