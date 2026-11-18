"""Small deployment surface; secrets remain in the platform credential boundary."""

from dataclasses import dataclass, field
from open_deep_research.platform.runtime import getenv


@dataclass(frozen=True)
class Settings:
    enabled: bool
    max_runs: int
    poll_seconds: float
    retention_days: int
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    public_origin: str
    notifications_enabled: bool = False


def settings():
    return Settings(str(getenv("INTELLIGENCE_ENABLED", "false")).lower() == "true",
        max(1, int(getenv("INTELLIGENCE_MAX_CONCURRENT_RUNS", "4"))),
        max(1, float(getenv("INTELLIGENCE_POLL_SECONDS", "5"))),
        max(1, int(getenv("INTELLIGENCE_SNAPSHOT_RETENTION_DAYS", "90"))),
        getenv("FEISHU_APP_ID", ""), getenv("FEISHU_APP_SECRET", ""),
        getenv("INTELLIGENCE_PUBLIC_ORIGIN", "").rstrip("/"),
        str(getenv("FEISHU_NOTIFICATIONS_ENABLED", "false")).lower() == "true")
