"""业务日期与面向用户的北京时间；内部事件和租约保留原始时刻。"""

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def business_time(value: datetime) -> str:
    """Show an instant in Shanghai at second precision. Naive database datetimes are UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def parse_business_time(value: str) -> datetime:
    """Read the Shanghai display format and earlier UTC/offset ISO timestamps."""
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=SHANGHAI if value[10:11] == " " else UTC)
    return instant


def business_time_fields(row, *extra_fields: str) -> dict:
    """Project the selected time columns of one API record without rewriting nested data."""
    result = dict(row)
    for field in ("create_time", "update_time", *extra_fields):
        value = result.get(field)
        if isinstance(value, str) and value[10:11] in {" ", "T"}:
            try:
                value = parse_business_time(value)
            except ValueError:
                continue
        if isinstance(value, datetime):
            result[field] = business_time(value)
    return result


def research_today() -> str:
    """按部署时区确定日期，默认与平台业务时区 Asia/Shanghai 一致。"""
    return datetime.now(ZoneInfo(os.getenv("TZ") or "Asia/Shanghai")).date().isoformat()
