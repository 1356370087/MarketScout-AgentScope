"""研究提示词的业务日期；事件与费用账本继续使用 UTC 时间戳。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo


def research_today() -> str:
    """按部署时区确定日期，默认与平台业务时区 Asia/Shanghai 一致。"""
    return datetime.now(ZoneInfo(os.getenv("TZ") or "Asia/Shanghai")).date().isoformat()
