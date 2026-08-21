"""Sophia — OpenAI-compatible gateway over ladyol's provider pool."""
from sophia.pool import SophiaPool
from sophia.server import create_app

__all__ = ["SophiaPool", "create_app"]
__version__ = "0.1.0"
