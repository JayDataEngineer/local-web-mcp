"""Utility modules for reducing code duplication"""

from .url_utils import extract_domain
from .singleton import create_singleton_factory, create_async_singleton_factory

__all__ = ["extract_domain", "create_singleton_factory", "create_async_singleton_factory"]
