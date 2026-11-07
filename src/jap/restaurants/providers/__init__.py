from .base import Provider, ProviderError
from .omakase import OmakaseProvider
from .pocketconcierge import PocketConciergeProvider
from .tabelog import TabelogProvider
from .tablecheck import TableCheckProvider

PROVIDERS: dict[str, type[Provider]] = {
    "tablecheck": TableCheckProvider,
    "tabelog": TabelogProvider,
    "pocketconcierge": PocketConciergeProvider,
    "omakase": OmakaseProvider,
}

__all__ = [
    "PROVIDERS",
    "Provider",
    "ProviderError",
    "TableCheckProvider",
    "TabelogProvider",
    "PocketConciergeProvider",
    "OmakaseProvider",
]
