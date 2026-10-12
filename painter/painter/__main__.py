import logging

import uvicorn

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run("painter.server:app", host=settings.host, port=settings.port, log_level="info")
