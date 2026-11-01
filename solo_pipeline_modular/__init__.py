"""Modular solo pipeline package."""
from __future__ import annotations

from .solo_pipeline import *

__all__ = [name for name in globals() if not name.startswith("__")]
