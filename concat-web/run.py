"""Start Concat Web on a dedicated loopback listener."""
import os
import uvicorn

if __name__ == "__main__":
    host = os.getenv("CONCAT_WEB_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1"}:
        raise SystemExit("Concat Web must listen on loopback only")
    port = int(os.getenv("CONCAT_WEB_PORT", "18873"))
    if port < 1024 or port > 65535:
        raise SystemExit("Invalid unprivileged port")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
