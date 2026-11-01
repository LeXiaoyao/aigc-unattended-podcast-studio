"""Shared Jinja2 templates instance with extra globals."""
import os
from pathlib import Path
from fastapi.templating import Jinja2Templates

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)

templates = Jinja2Templates(directory=str(ROOT_DIR / "webapp" / "templates"))
templates.env.globals.update({
    "enumerate": enumerate,
    "zip": zip,
    "len": len,
    "range": range,
})
