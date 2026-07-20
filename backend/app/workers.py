from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.config import get_settings
from app.core.logging import configure_logging


async def worker_health(_ctx: dict[str, Any]) -> str:
    return "ok"


settings = get_settings()
configure_logging(settings.log_level)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [worker_health]
    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(str(settings.redis_url))
    health_check_interval: ClassVar[int] = 30
    job_timeout: ClassVar[int] = 300
    max_jobs: ClassVar[int] = 2
