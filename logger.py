import logging
import sys
from pathlib import Path

def _make_logger() -> logging.Logger:
    if getattr(sys, "frozen", False):
        log_path = Path(sys.executable).parent / "neuralchat.log"
        h: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        h = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[h],
    )
    return logging.getLogger("neuralchat")

logger = _make_logger()
