"""Email Agent starter package."""

from .agent import EmailAgent
from .config import Settings, get_settings

__all__ = ["EmailAgent", "Settings", "get_settings", "__version__"]
__version__ = "0.1.0"
