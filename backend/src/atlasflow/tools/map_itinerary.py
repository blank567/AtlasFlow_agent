"""Bounded multi-stop navigation; only navigation links survive the call."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlasflow.tools.base import Capability, ToolContext, ToolResult
from atlasflow.tools.map_route import AmapRouteArguments, AmapRouteTool


class MapItineraryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    city: str = Field(min_length=1, max_length=80)
    stops: list[str] = Field(
        min_length=2,
        max_length=6,
        description="按顺序填写2至6个站点的完整名称；需要返回起点时在末尾重复起点",
    )
    mode: Literal["driving", "walking"]

    @model_validator(mode="after")
    def validate_stops(self):
        if any(not stop.strip() or len(stop) > 120 for stop in self.stops):
            raise ValueError("stops must have 1 to 120 characters")
        self.stops = [stop.strip() for stop in self.stops]
        if any(a == b for a, b in zip(self.stops, self.stops[1:])):
            raise ValueError("adjacent stops must differ")
        return self


class MapItineraryTool(AmapRouteTool):
    name = "map_itinerary"
    description = "一次查询2至6个有序站点的全部相邻路段。游览多个景点并返回学校时使用此工具；末尾重复起点。只保留逐段导航链接。"
    capabilities = (
        Capability("map_itinerary", "多站点顺序行程及返回起点的逐段导航；最多6站点", True),
    )
    arguments_model = MapItineraryArguments

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
        return "全部相邻路段已核验；打开各导航链接查看最新路线" if result.success else None

    async def run(self, arguments: MapItineraryArguments, context: ToolContext) -> ToolResult:
        links = []
        for index, (origin, destination) in enumerate(zip(arguments.stops, arguments.stops[1:]), 1):
            leg = await super().run(
                AmapRouteArguments(
                    city=arguments.city, origin=origin, destination=destination, mode=arguments.mode
                ),
                context,
            )
            if not leg.success:
                return ToolResult(
                    success=False,
                    error=f"第 {index} 段失败：{leg.error}",
                    data={"provider": "amap", "navigation_urls": links},
                    retryable=leg.retryable,
                )
            links.append(leg.data["navigation_url"])
        # Distances and durations never leave this aggregate tool.
        return ToolResult(success=True, data={"provider": "amap", "navigation_urls": links})
