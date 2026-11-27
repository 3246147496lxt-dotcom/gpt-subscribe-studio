"""结账链接生成能力服务。"""

from __future__ import annotations

from .adapter import (
    CheckoutAdapterBase,
    FakeCheckoutAdapter,
    UpstreamCheckoutAdapter,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "CheckoutAdapterBase",
    "UpstreamCheckoutAdapter",
    "FakeCheckoutAdapter",
]
