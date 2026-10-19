"""只接受可归属的 LiteLLM 请求账单，未知用量不按零费用处理。"""

from decimal import Decimal
import os
import httpx
from sqlalchemy import select
from . import schema as s, quota
from .store import get_store


async def reconcile_service_charges():
    store = get_store()
    async with store.engine.connect() as conn:
        rows = (await conn.execute(select(s.charges).where(s.charges.c.status == "unknown",
            s.charges.c.operation_id.not_like("run:%")).order_by(s.charges.c.create_time).limit(100))).mappings().all()
    if not rows:
        return {"checked": 0, "settled": 0, "unresolved": 0}
    base = os.getenv("LITELLM_BASE_URL", "").rstrip("/").removesuffix("/v1")
    key = os.getenv("LITELLM_MASTER_KEY", "")
    resolved = 0
    if base and key:
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + key}, timeout=20) as client:
            for row in rows:
                response = await client.get("/spend/logs", params={"request_id": row["operation_id"]})
                response.raise_for_status()
                data = response.json()
                entries = data if isinstance(data, list) else data.get("data", data.get("logs", []))
                matched = [entry for entry in entries if entry.get("request_id") == row["operation_id"] and entry.get("spend") is not None]
                if len(matched) != 1:
                    continue
                amount = Decimal(str(matched[0]["spend"]))
                if not amount.is_finite() or amount < 0:
                    continue
                await quota.correct(row["operation_id"], int((amount * 1_000_000).to_integral_value()))
                resolved += 1
    return {"checked": len(rows), "settled": resolved, "unresolved": len(rows)-resolved}
