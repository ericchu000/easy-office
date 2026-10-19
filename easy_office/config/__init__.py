"""Dynamic configuration center."""

from easy_office.config.manager import ConfigManager
from easy_office.config.models import AppConfig

__all__ = ["AppConfig", "ConfigManager"]
