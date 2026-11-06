from .base import Provider, ProviderError
from .omakase import OmakaseProvider
from .tabelog import TabelogProvider
from .tablecheck import TableCheckProvider

PROVIDERS: dict[str, type[Provider]] = {
    "tablecheck": TableCheckProvider,
    "tabelog": TabelogProvider,
    "omakase": OmakaseProvider,
}

__all__ = ["PROVIDERS", "Provider", "ProviderError", "TableCheckProvider", "TabelogProvider", "OmakaseProvider"]
