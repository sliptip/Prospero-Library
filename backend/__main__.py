import os

import uvicorn
from dotenv import load_dotenv

from .database import APP_DIRECTORY


def main():
    load_dotenv(APP_DIRECTORY / ".env")
    port = int(os.getenv("PROSPERO_PORT", "8000"))
    host = os.getenv("PROSPERO_HOST", "127.0.0.1")
    if not 1 <= port <= 65535:
        raise SystemExit("PROSPERO_PORT должен быть от 1 до 65535")
    if host not in {"127.0.0.1", "::1", "localhost"} and not os.getenv("PROSPERO_ALLOWED_HOSTS"):
        raise SystemExit("Для LAN перечислите IP или имена в PROSPERO_ALLOWED_HOSTS")
    uvicorn.run("backend.main:app", host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
