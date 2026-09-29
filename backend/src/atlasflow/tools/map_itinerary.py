"""Bounded multi-stop navigation; only navigation links survive the call."""

from itertools import pairwise
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atlasflow.tools.base import Capability, ToolContext, ToolResult
from atlasflow.tools.map_route import (
    AmapPlace,
    AmapRouteTool,
    MapServiceError,
    PlaceInput,
    normalize_amap_city,
)


class MapItineraryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    city: str = Field(min_length=1, max_length=80)
    stops: list[PlaceInput] = Field(
        min_length=2,
        max_length=6,
        description="按顺序填写2至6个结构化地点（name/district/address/entrance），未知条件留空；兼容完整名称字符串。返回起点时末尾重复起点",
    )
    mode: Literal["driving", "walking"]

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str) -> str:
        return normalize_amap_city(value)

    @model_validator(mode="after")
    def validate_stops(self):
        self.stops = [stop.strip() if isinstance(stop, str) else stop for stop in self.stops]
        normalized = [
            AmapPlace(name=stop) if isinstance(stop, str) else stop for stop in self.stops
        ]
        if any(a == b for a, b in pairwise(normalized)):
            raise ValueError("adjacent stops must differ")
        return self


class MapItineraryTool(AmapRouteTool):
    name = "map_itinerary"
    description = "先预解析2至6个有序站点的 POI，按最高匹配得分选择，再用高德正式名称/坐标查询全部相邻路段。优先使用结构化地点，填写已知区县/地址/入口，不得猜测。返回起点时末尾重复起点。只保留逐段导航链接。"
    capabilities = (
        Capability("map_itinerary", "多站点顺序行程及返回起点的逐段导航；最多6站点", True),
    )
    arguments_model = MapItineraryArguments
    max_executions_per_stage = 1

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        stops = arguments.get("stops")
        return {
            "mode": str(arguments.get("mode", ""))[:20],
            "stop_count": len(stops) if isinstance(stops, list) else 0,
        }

    @staticmethod
    def navigation_urls(result: ToolResult) -> list[str]:
        return list(result.data.get("navigation_urls", []))

    @staticmethod
    def navigation_url(result: ToolResult) -> str | None:
        return next(iter(result.data.get("navigation_urls", [])), None)

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        return "全部站点已按最高匹配得分预解析，全部相邻路段已核验；打开导航核对正式地点与最新路线" if result.success else None

    async def run(self, arguments: MapItineraryArguments, context: ToolContext) -> ToolResult:
        if not self._api_key:
            return ToolResult(success=False, error="AMAP_API_KEY 未配置")
        links = []
        phase = "POI 预解析"
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=12, transport=self._transport) as client:
                resolved = []
                local_places = {}  # Ephemeral within this invocation; never persisted or traced.
                for index, stop in enumerate(arguments.stops, 1):
                    phase = f"POI 预解析第 {index} 站"
                    place = AmapPlace(name=stop) if isinstance(stop, str) else stop
                    key = place.model_dump_json()
                    if key not in local_places:
                        local_places[key] = await self._resolve(client, arguments.city, place, label=f"第 {index} 站")
                    resolved.append(local_places[key])
                # No route request occurs before every stop has a resolved official POI.
                for index, (origin, destination) in enumerate(pairwise(resolved), 1):
                    phase = f"第 {index} 段"
                    leg = await self._query_leg(client, origin, destination, arguments.mode, arguments.city)
                    if not leg.success:
                        return ToolResult(success=False, error=f"{phase}失败：{leg.error}",
                                          data={"provider": "amap", "navigation_urls": links}, retryable=leg.retryable)
                    links.append(leg.data["navigation_url"])
        except MapServiceError as exc:
            error, retryable = str(exc), False
        except httpx.TimeoutException:
            error, retryable = "AMAP_TIMEOUT: 高德请求超时", True
        except httpx.HTTPStatusError as exc:
            error, retryable = f"AMAP_HTTP_{exc.response.status_code}: 高德 HTTP 请求失败", False
        except httpx.HTTPError:
            error, retryable = "AMAP_NETWORK: 无法连接高德服务", True
        except (ValueError, TypeError, KeyError):
            error, retryable = "AMAP_INVALID_RESPONSE: 高德返回数据格式异常", False
        else:
            return ToolResult(success=True, data={"provider": "amap", "navigation_urls": links})
        # Distances and durations never leave this aggregate tool.
        return ToolResult(success=False, error=f"{phase}失败：{error}",
                          data={"provider": "amap", "navigation_urls": links}, retryable=retryable)
