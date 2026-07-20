from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container listener; only Caddy publishes ports
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxy_ips),
        access_log=False,
        log_config="logging.json",
    )


if __name__ == "__main__":
    main()
