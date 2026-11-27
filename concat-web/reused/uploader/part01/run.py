from __future__ import annotations
import uvicorn
from lum_engine.config import WEB_HOST, WEB_PORT

if __name__ == "__main__":
    uvicorn.run("lum_engine.standalone_app:app", host=WEB_HOST, port=WEB_PORT, workers=1, log_level="info", proxy_headers=True)
