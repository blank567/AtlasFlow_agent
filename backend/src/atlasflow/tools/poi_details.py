"""AMap POI details for one-time decision support, never persisted as facts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.schemas import Evidence
from atlasflow.tools.base import Capability, RiskLevel, ToolContext, ToolResult
from atlasflow.tools.map_route import AmapRequestLimiter, AmapRouteTool, MapServiceError


class PoiDetailsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(min_length=1, max_length=80, description="明确的中国城市或区县")
    place: str = Field(min_length=1, max_length=120, description="景点或场馆全称")


class PoiDetailsTool(AmapRouteTool):
    name = "poi_details"
    description = (
        "临时核对中国境内景点或场馆的高德 POI 商业字段。"
        "营业时间可能变化，平均消费不是门票价格；预约、门票与最新营业安排应另查官方网页。"
    )
    risk_level = RiskLevel.LOW
    arguments_model = PoiDetailsArguments
    capabilities = (Capability("poi_details", "当次核对国内地点及可能的营业信息", True),)
    allowed_agents = ("researcher", "critic", "quality_gate")
    transient_output = True
    transient_notice = (
        "高德 POI 数据仅供当次判断，不得写入报告、RunRecord、Event 或 Trace；"
        "营业时间、预约与票价请以场馆官网为准。"
    )

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        request_limiter: AmapRequestLimiter | None = None,
    ) -> None:
        super().__init__(api_key, transport=transport, request_limiter=request_limiter)

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"lookup": "poi_details"}

    @staticmethod
    def safe_evidence(evidence: Sequence[Evidence]) -> list[Evidence]:
        return []

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        return (
            "地点已临时核对；营业时间、预约和票价须查看场馆官方渠道，"
            "高德字段不可写入报告。"
            if result.success
            else None
        )

    async def run(self, arguments: PoiDetailsArguments, context: ToolContext) -> ToolResult:
        if not self._api_key:
            return ToolResult(success=False, error="AMAP_API_KEY 未配置")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=12,
                transport=self._transport,
            ) as client:
                data = await self._get_json(
                    client,
                    "/v5/place/text",
                    {
                        "key": self._api_key,
                        "keywords": arguments.place,
                        "region": arguments.city,
                        "city_limit": "true",
                        "page_size": "5",
                        "page_num": "1",
                        "show_fields": "business",
                    },
                )
        except MapServiceError as exc:
            return ToolResult(success=False, error=str(exc))
        except httpx.TimeoutException:
            return ToolResult(success=False, error="AMAP_TIMEOUT: 高德请求超时", retryable=True)
        except httpx.HTTPStatusError as exc:
            return ToolResult(success=False, error=f"AMAP_HTTP_{exc.response.status_code}: 高德请求失败")
        except httpx.HTTPError:
            return ToolResult(success=False, error="AMAP_NETWORK: 无法连接高德服务", retryable=True)
        except (ValueError, TypeError):
            return ToolResult(success=False, error="AMAP_INVALID_RESPONSE: 高德返回数据格式异常")

        raw = data.get("pois")
        pois = [row for row in raw if isinstance(row, dict) and isinstance(row.get("name"), str)] if isinstance(raw, list) else []
        exact = [row for row in pois if self._place_name(row["name"]) == self._place_name(arguments.place)]
        if not pois:
            return ToolResult(success=False, error="AMAP_POI_NOT_FOUND: 未找到地点，请补充全称")
        if len(exact) != 1:
            return ToolResult(success=False, error="AMAP_POI_AMBIGUOUS: 地点候选不唯一，请提供更具体的名称")
        poi = exact[0]
        business = poi.get("business") if isinstance(poi.get("business"), dict) else {}
        return ToolResult(
            success=True,
            data={
                "provider": "amap",
                "name": poi["name"],
                "address": poi.get("address"),
                "business": {
                    "opentime_today": business.get("opentime_today"),
                    "opentime_week": business.get("opentime_week"),
                    "tel": business.get("tel"),
                    "average_cost_not_ticket_price": business.get("cost"),
                },
                "queried_at": datetime.now(UTC).isoformat(),
                "notice": "高德结果仅供当次判断；营业、预约、票价以场馆官网为准。",
            },
        )
