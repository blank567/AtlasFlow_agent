"""Map route tool using AMap; provider content stays transient."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, RiskLevel, ToolContext, ToolResult


class AmapRouteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(min_length=1, max_length=80, description="明确的中国城市或区县")
    origin: str = Field(min_length=1, max_length=120, description="起点名称或地址")
    destination: str = Field(min_length=1, max_length=120, description="终点名称或地址")
    mode: Literal["driving", "walking"] = Field(description="驾车或步行")


class MapServiceError(RuntimeError):
    """Only application-generated, credential-free diagnostics may enter this exception."""


class AmapRequestLimiter:
    """Serialize request starts across route tools sharing one registry."""

    def __init__(self, interval: float = 1.05) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_started = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.interval - (loop.time() - self._last_started)))
            self._last_started = loop.time()


class AmapRouteTool(BaseTool):
    allowed_agents = ("researcher", "critic", "quality_gate")
    transient_output = True
    transient_notice = "路线数字仅供本次判断，报告只放导航链接并提示查看最新路线"
    capabilities = (Capability("map_route", "查询中国境内驾车或步行路线和导航入口", True),)
    name = "map_route"
    description = (
        "查询中国境内明确城市的两地点驾车或步行路线，返回本次距离、耗时和高德导航入口。"
        "同名地点必须给出城市；结果仅供参考。"
    )
    risk_level = RiskLevel.LOW
    arguments_model = AmapRouteArguments
    base_url: ClassVar[str] = "https://restapi.amap.com"

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 request_limiter: AmapRequestLimiter | None = None) -> None:
        self._api_key = api_key.strip()
        self._transport = transport
        self._limiter = request_limiter or AmapRequestLimiter(0 if transport else 1.05)

    def unavailable_reason(self) -> str | None:
        return None if self._api_key else "AMAP_API_KEY 未配置"

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"mode": str(arguments.get("mode", ""))[:20]}

    @staticmethod
    def safe_evidence(evidence: Sequence[Evidence]) -> list[Evidence]:
        return []

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        return "路线已查询；打开导航链接查看最新距离与耗时" if result.success else None

    @staticmethod
    def navigation_url(result: ToolResult) -> str | None:
        value = result.data.get("navigation_url")
        return str(value) if result.success and value else None

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
                return await self._query_leg(
                    client, origin, destination, arguments.mode, arguments.city
                )
        except MapServiceError as exc:
            return ToolResult(success=False, error=str(exc))
        except httpx.TimeoutException:
            return ToolResult(success=False, error="AMAP_TIMEOUT: 高德请求超时", retryable=True)
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                success=False, error=f"AMAP_HTTP_{exc.response.status_code}: 高德 HTTP 请求失败"
            )
        except httpx.HTTPError:
            return ToolResult(success=False, error="AMAP_NETWORK: 无法连接高德服务", retryable=True)
        except (ValueError, TypeError, KeyError):
            return ToolResult(success=False, error="AMAP_INVALID_RESPONSE: 高德返回数据格式异常")

    async def _query_leg(
        self,
        client: httpx.AsyncClient,
        origin: dict[str, str],
        destination: dict[str, str],
        travel_mode: str,
        city: str,
    ) -> ToolResult:
        route = await self._get_json(
            client,
            f"/v5/direction/{travel_mode}",
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
            raise MapServiceError("AMAP_NO_ROUTE: 高德未返回可用路线")
        first = paths[0]
        cost = first.get("cost") if isinstance(first.get("cost"), dict) else {}
        distance = self._positive_int(first.get("distance"))
        duration = self._positive_int(cost.get("duration") or first.get("duration"))
        if distance is None or duration is None:
            raise MapServiceError("AMAP_INCOMPLETE_ROUTE: 高德路线缺少距离或预计耗时")
        mode = "car" if travel_mode == "driving" else "walk"
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
                "city": city,
                "origin": origin["name"],
                "destination": destination["name"],
                "mode": travel_mode,
                "distance_m": distance,
                "duration_s": duration,
                "navigation_url": link,
                "coordinate_system": "GCJ-02",
                "queried_at": datetime.now(UTC).isoformat(),
                "notice": "路线仅供参考，请打开高德导航查看最新路径、耗时和交通状况",
            },
        )

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
        if not pois:
            raise MapServiceError("AMAP_POI_NOT_FOUND: 未找到地点，请补充城市、地点全称或入口")
        exact = [item for item in pois if self._place_name(item["name"]) == self._place_name(name)]
        choices = exact or pois
        if len(choices) != 1:
            raise MapServiceError("AMAP_POI_AMBIGUOUS: 地点存在多个候选，请使用地点全称或具体入口")
        return {
            "name": choices[0]["name"],
            "location": choices[0]["location"],
            "id": str(choices[0].get("id") or ""),
        }

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: dict[str, str]
    ) -> dict[str, object]:
        data: Any = None
        for attempt in range(3):
            await self._limiter.wait()
            response = await client.get(path, params=params)
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(1 + attempt)
                continue
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and str(data.get("infocode")) in {"10004", "10021"} and attempt < 2:
                await asyncio.sleep(1 + attempt)
                continue
            break
        if not isinstance(data, dict):
            raise MapServiceError("AMAP_INVALID_RESPONSE: 高德返回非对象数据")
        if str(data.get("status")) != "1":
            code = str(data.get("infocode", "unknown"))
            code = code if code.isdigit() and len(code) <= 6 else "unknown"
            reasons = {
                "10001": "Key 无效",
                "10009": "Key 平台不匹配，请使用 Web 服务 Key",
                "10003": "日配额超限",
                "10004": "账号访问频率超限",
                "10005": "IP 白名单限制",
                "10006": "绑定域名无效",
                "10010": "IP 请求超限",
                "10021": "QPS 超限",
                "10002": "服务无权限或接口路径错误",
                "10012": "服务权限不足",
            }
            raise MapServiceError(
                f"AMAP_{code}: {reasons.get(code, '高德拒绝请求，请根据错误码检查权限及参数')}"
            )
        return data

    @staticmethod
    def _place_name(name: str) -> str:
        # Only explicit equivalent names, never fuzzy ranking or first-result selection.
        value = unicodedata.normalize("NFKC", name).strip()
        value = value.replace("圆明园遗址公园", "圆明园")
        if value == "北大":
            value = "北京大学"
        return "".join(char for char in value if not char.isspace() and char not in "()-·")

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(str(value))
        except (ValueError, TypeError):
            return None
        return parsed if parsed > 0 else None
