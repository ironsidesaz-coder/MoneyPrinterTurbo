"""测试套件级别的公共夹具。"""

import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def _no_live_wikimedia_fallback(monkeypatch):
    """
    Pexels/Pixabay 结果不足时默认会回退到 Wikimedia Commons。测试必须保持
    离线且结果确定，因此默认让后备搜索返回空列表；需要验证后备逻辑的测试
    自行 patch ``search_videos_wikimedia`` 或直接调用 ``wikimedia_commons``。
    """
    # 只在测试模块已导入素材服务时打补丁，避免夹具本身提前加载配置
    # （首次导入 app.config 可能从示例生成 config.toml）。
    material = sys.modules.get("app.services.material")
    if material is None:
        yield
        return
    monkeypatch.setattr(material, "search_videos_wikimedia", lambda *a, **k: [])
    yield
