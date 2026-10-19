"""Bounded multi-stop navigation; only navigation links survive the call."""

from itertools import pairwise
from typing import Annotated, Any, Literal

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
        default_factory=list,
        max_length=6,
        description="单日行程按顺序填写2至6个结构化地点，未知条件留空；多日行程请使用days而非拼成一条长路线",
    )
    days: list[Annotated[list[PlaceInput], Field(min_length=2, max_length=6)]] = Field(
        default_factory=list, max_length=3,
        description="按日期顺序填写1至3天的站点列表，每天2至6站；只核验每日内部相邻路段，不连接跨日地点。与stops二选一",
    )
    mode: Literal["driving", "walking"]

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str) -> str:
        return normalize_amap_city(value)

    @model_validator(mode="after")
    def validate_stops(self):
        if bool(self.stops) == bool(self.days):
            raise ValueError("provide either stops or days, not both")
        for stops in self.days or [self.stops]:
            if not 2 <= len(stops) <= 6:
                raise ValueError("each day requires 2 to 6 stops")
            normalized = [AmapPlace(name=stop.strip()) if isinstance(stop, str) else stop for stop in stops]
            if any(a == b for a, b in pairwise(normalized)):
                raise ValueError("adjacent stops must differ")
        return self


class MapItineraryTool(AmapRouteTool):
    name = "map_itinerary"
    description = "先预解析全部站点的POI，按最高匹配得分选择，再用高德正式名称/坐标核验相邻路段。单日用stops（2至6站），多日用days（最多3天，每天2至6站，不连接跨日地点），二选一。填写已知区县/地址/入口，不得猜测。只保留逐段导航链接；未解析地点不会被擅自跳过或替换。"
    capabilities = (
        Capability("map_itinerary", "按日分组的逐段导航；最多3天、每天2至6站点", True),
    )
    arguments_model = MapItineraryArguments
    max_executions_per_stage = 1

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        stops = arguments.get("stops")
        days = arguments.get("days")
        return {
            "mode": str(arguments.get("mode", ""))[:20],
            "stop_count": sum(len(day) for day in days if isinstance(day, list)) if isinstance(days, list) and days else len(stops) if isinstance(stops, list) else 0,
            "day_count": len(days) if isinstance(days, list) and days else 1,
        }

    @staticmethod
    def navigation_urls(result: ToolResult) -> list[str]:
        return list(result.data.get("navigation_urls", []))

    @staticmethod
    def navigation_url(result: ToolResult) -> str | None:
        return next(iter(result.data.get("navigation_urls", [])), None)

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        if result.success:
            return (f"{result.data.get('day_count', 1)}天行程全部站点已按最高匹配得分预解析，"
                    "每日内部全部相邻路段已核验；打开导航核对正式地点与最新路线")
        verified = result.data.get("verified_legs", [])
        if verified:
            return (f"仅第{'、'.join(map(str, verified))}段已核验，共{result.data['total_legs']}段；"
                    "其余路段未核实，不得声称行程导航完整，打开链接查看最新路线")
        return None

    async def run(self, arguments: MapItineraryArguments, context: ToolContext) -> ToolResult:
        if not self._api_key:
            return ToolResult(success=False, error="AMAP_API_KEY 未配置")
        links = []
        verified_legs: list[int] = []
        failures: list[str] = []
        retryable = False
        groups = arguments.days or [arguments.stops]
        total_legs = sum(len(stops) - 1 for stops in groups)
        phase = "POI 预解析"
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=12, transport=self._transport) as client:
                resolved_groups = []
                local_places = {}  # Ephemeral within this invocation; never persisted or traced.
                local_failures: dict[str, str] = {}
                for day, stops in enumerate(groups, 1):
                    resolved = []
                    for index, stop in enumerate(stops, 1):
                        label = f"第{day}天第 {index} 站" if len(groups) > 1 else f"第 {index} 站"
                        phase = f"POI 预解析{label}"
                        place = AmapPlace(name=stop) if isinstance(stop, str) else stop
                        key = place.model_dump_json()
                        if key not in local_places:
                            try:
                                local_places[key] = await self._resolve(client, arguments.city, place, label="地点")
                            except MapServiceError as exc:
                                # Keep unrelated valid legs without routing
                                # across an unresolved station.
                                if not str(exc).startswith(("AMAP_POI_NO_MATCH:", "AMAP_POI_NOT_FOUND:")):
                                    raise
                                local_places[key] = None
                                local_failures[key] = str(exc)
                        if local_places[key] is None:
                            failures.append(f"{phase}失败：{local_failures[key]}；未替换或跳过该站")
                        resolved.append(local_places[key])
                    resolved_groups.append(resolved)
                # Preflight every station before any route request. Only
                # original adjacent pairs with TWO verified endpoints qualify.
                pairs = (pair for resolved in resolved_groups for pair in pairwise(resolved))
                for index, (origin, destination) in enumerate(pairs, 1):
                    if origin is None or destination is None:
                        continue
                    phase = f"第 {index} 段"
                    try:
                        leg = await self._query_leg(client, origin, destination, arguments.mode, arguments.city)
                    except MapServiceError as exc:
                        if not str(exc).startswith(("AMAP_NO_ROUTE:", "AMAP_INCOMPLETE_ROUTE:")):
                            raise
                        failures.append(f"{phase}失败：{exc}")
                        continue
                    if not leg.success:
                        failures.append(f"{phase}失败：{leg.error}")
                        retryable |= leg.retryable
                        continue
                    links.append(leg.data["navigation_url"])
                    verified_legs.append(index)
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
            return ToolResult(success=not failures, error="；".join(failures) or None,
                              retryable=retryable,
                              data={"provider": "amap", "navigation_urls": links,
                                    "verified_legs": verified_legs, "total_legs": total_legs, "day_count": len(groups)})
        # Distances and durations never leave this aggregate tool.
        return ToolResult(success=False, error=f"{phase}失败：{error}",
                          data={"provider": "amap", "navigation_urls": links,
                                "verified_legs": verified_legs, "total_legs": total_legs, "day_count": len(groups)}, retryable=retryable)
