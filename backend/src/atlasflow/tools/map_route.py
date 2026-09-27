"""Map route tool using AMap; provider content stays transient."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class AmapRouteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(min_length=1, max_length=80, description="明确的中国城市或区县")
    origin: str = Field(min_length=1, max_length=120, description="起点名称或地址")
    destination: str = Field(min_length=1, max_length=120, description="终点名称或地址")
    mode: Literal["driving", "walking"] = Field(description="驾车或步行")


class MapServiceError(RuntimeError):
    pass


class AmapRouteTool(BaseTool):
    name = "map_route"
    description = (
        "查询中国境内明确城市的两地点驾车或步行路线，返回本次距离、耗时和高德导航入口。"
        "同名地点必须给出城市；结果仅供参考。"
    )
    risk_level = RiskLevel.LOW
    arguments_model = AmapRouteArguments
    base_url: ClassVar[str] = "https://restapi.amap.com"

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key.strip()
        self._transport = transport

    async def run(self, arguments: AmapRouteArguments, context: ToolContext) -> ToolResult:
        if not self._api_key:
            return ToolResult(success=False, error="AMAP_API_KEY 未配置")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=12,
                transport=self._transport,
            ) as client:
                origin = await self._resolve(client, arguments.city, arguments.origin)
                destination = await self._resolve(client, arguments.city, arguments.destination)
                route = await self._get_json(
                    client,
                    f"/v5/direction/{arguments.mode}",
                    {
                        "key": self._api_key,
                        "origin": origin["location"],
                        "destination": destination["location"],
                        "origin_id": origin.get("id", ""),
                        "destination_id": destination.get("id", ""),
                        "show_fields": "cost",
                    },
                )
            route_data = route.get("route")
            paths = route_data.get("paths", []) if isinstance(route_data, dict) else []
            if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
                raise MapServiceError("高德未返回可用路线")
            first = paths[0]
            cost = first.get("cost") if isinstance(first.get("cost"), dict) else {}
            distance = self._positive_int(first.get("distance"))
            duration = self._positive_int(cost.get("duration") or first.get("duration"))
            if distance is None or duration is None:
                raise MapServiceError("高德路线缺少距离或预计耗时")
            mode = "car" if arguments.mode == "driving" else "walk"
            link = "https://uri.amap.com/navigation?" + urlencode(
                {
                    "from": f"{origin['location']},{origin['name']}",
                    "to": f"{destination['location']},{destination['name']}",
                    "mode": mode,
                    "src": "atlasflow",
                    "callnative": "0",
                }
            )
            return ToolResult(
                success=True,
                data={
                    "provider": "amap",
                    "city": arguments.city,
                    "origin": origin["name"],
                    "destination": destination["name"],
                    "mode": arguments.mode,
                    "distance_m": distance,
                    "duration_s": duration,
                    "navigation_url": link,
                    "coordinate_system": "GCJ-02",
                    "queried_at": datetime.now(UTC).isoformat(),
                    "notice": "路线仅供参考，请打开高德导航查看最新路径、耗时和交通状况",
                },
            )
        except (MapServiceError, httpx.HTTPError, ValueError, TypeError):
            # Provider URLs carry the key; never persist or trace raw exceptions.
            return ToolResult(success=False, error="地图查询失败或地点不明确，请检查城市和起终点")

    async def _resolve(self, client: httpx.AsyncClient, city: str, name: str) -> dict[str, str]:
        data = await self._get_json(
            client,
            "/v5/place/text",
            {
                "key": self._api_key,
                "keywords": name,
                "region": city,
                "city_limit": "true",
                "page_size": "3",
                "page_num": "1",
            },
        )
        raw = data.get("pois", [])
        pois = (
            [
                item
                for item in raw
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("location"), str)
                and len(item["location"].split(",")) == 2
            ]
            if isinstance(raw, list)
            else []
        )
        exact = [item for item in pois if item["name"].strip() == name.strip()]
        choices = exact or pois
        if len(choices) != 1:
            raise MapServiceError("地点无唯一匹配")
        return {
            "name": choices[0]["name"],
            "location": choices[0]["location"],
            "id": str(choices[0].get("id") or ""),
        }

    @staticmethod
    async def _get_json(
        client: httpx.AsyncClient, path: str, params: dict[str, str]
    ) -> dict[str, object]:
        response = await client.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or str(data.get("status")) != "1":
            raise MapServiceError("高德服务未接受查询")
        return data

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(str(value))
        except (ValueError, TypeError):
            return None
        return parsed if parsed > 0 else None
