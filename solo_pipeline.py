#!/usr/bin/env python3
"""Compatibility wrapper for the modular solo pipeline package."""
from __future__ import annotations

import sys

from solo_pipeline_modular.shared import *
from solo_pipeline_modular.solo_llm import *
from solo_pipeline_modular.solo_news import *
from solo_pipeline_modular.solo_script import *
from solo_pipeline_modular.solo_title import *
from solo_pipeline_modular.solo_shownotes import *
from solo_pipeline_modular.solo_publish import *
from solo_pipeline_modular.solo_pipeline import *


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
