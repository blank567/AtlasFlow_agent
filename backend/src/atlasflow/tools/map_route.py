"""Map route tool using AMap; provider content stays transient."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, RiskLevel, ToolContext, ToolResult


class AmapPlace(BaseModel):
    """Intent supplied by the agent, never invented provider IDs or coordinates."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120, description="地点全称，不包含猜测的分店或入口")
    district: str | None = Field(
        default=None, min_length=1, max_length=40, description="已知区县全称；未知留空"
    )
    address: str | None = Field(
        default=None, min_length=1, max_length=120, description="已知街道门牌；未知留空"
    )
    entrance: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        description="用户指定或行程有依据选择的入口，如东门；未知留空，不能编造",
    )


PlaceInput = AmapPlace | Annotated[str, Field(min_length=1, max_length=120)]


class AmapRouteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(
        min_length=1,
        max_length=80,
        description="中国城市全称或行政区划代码；区县写在地点的 district 中",
    )
    origin: PlaceInput = Field(
        description="起点：优先提供 name/district/address/entrance 对象；兼容完整名称字符串"
    )
    destination: PlaceInput = Field(
        description="终点：优先提供 name/district/address/entrance 对象；兼容完整名称字符串"
    )
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
        "优先使用结构化起终点，补充已知区县、地址和入口；不要猜测入口、POI ID 或坐标。"
        "先进行 POI 预解析，按匹配得分选择最高有效候选，再用高德正式名称和坐标查询路线。"
        "无有效候选时根据端点提示修正条件，不要原样重试。自动选择仅供参考，请核对导航地点。"
    )
    risk_level = RiskLevel.LOW
    arguments_model = AmapRouteArguments
    base_url: ClassVar[str] = "https://restapi.amap.com"

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        request_limiter: AmapRequestLimiter | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._transport = transport
        self._limiter = request_limiter or AmapRequestLimiter(0 if transport else 1.05)

    def unavailable_reason(self) -> str | None:
        return None if self._api_key else "AMAP_API_KEY 未配置"

    def validate_context(self, arguments: dict[str, Any], context: ToolContext) -> str | None:
        # A narrow safety check for explicit departure gates, NOT semantic tool routing.
        gates = re.findall(r"从([^，。；\n,;]{1,40}?(?:东|西|南|北)(?:[一二三四五六七八九0-9])?门)出发", context.user_query)
        places = arguments.get("stops", [arguments.get("origin"), arguments.get("destination")])
        for gate in gates:
            expected = self._place_name(gate.replace("北大", "北京大学"))
            expected = self._local_name(expected, arguments.get("city", ""), None)
            base = re.sub(r"(?:东|西|南|北)(?:[一二三四五六七八九0-9])?门$", "", expected)
            for place in places:
                if isinstance(place, str):
                    name, entrance = place, ""
                elif isinstance(place, dict):
                    name, entrance = place.get("name", ""), place.get("entrance") or ""
                else:
                    continue
                actual = self._place_name(name.replace("北大", "北京大学"))
                if entrance and not actual.endswith(self._place_name(entrance)):
                    actual += self._place_name(entrance)
                actual = self._local_name(actual, arguments.get("city", ""), None)
                is_same_site = actual == base or actual.startswith(expected) or bool(re.fullmatch(re.escape(base) + r"(?:东|西|南|北)[一二三四五六七八九0-9]?门.*", actual))
                if base and is_same_site and actual != expected:
                    return "MAP_LOCATION_CONSTRAINT: 用户明确指定的出发入口不可替换为场所整体、其他门或同名地铁站；请保留原入口，无法核验时报告缺口"
        return None

    @staticmethod
    def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"mode": str(arguments.get("mode", ""))[:20]}

    @classmethod
    def failure_scope(cls, arguments: dict[str, Any]) -> str | None:
        # Persist only a fingerprint: safe_arguments intentionally removes place text.
        try:
            normalized = cls.arguments_model.model_validate(arguments).model_dump(mode="json")
        except ValueError:
            return None
        for key in ("origin", "destination"):
            if isinstance(normalized.get(key), str):
                normalized[key] = AmapPlace(name=normalized[key]).model_dump()
        if isinstance(normalized.get("stops"), list):
            normalized["stops"] = [
                AmapPlace(name=item).model_dump() if isinstance(item, str) else item
                for item in normalized["stops"]
            ]
        value = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def safe_evidence(evidence: Sequence[Evidence]) -> list[Evidence]:
        return []

    @staticmethod
    def safe_summary(result: ToolResult) -> str | None:
        return "已按最高匹配得分预解析 POI 并查询路线；请打开导航核对正式地点和最新路线" if result.success else None

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
                origin = await self._resolve(client, arguments.city, arguments.origin, label="起点")
                destination = await self._resolve(
                    client, arguments.city, arguments.destination, label="终点"
                )
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

    async def _resolve(
        self, client: httpx.AsyncClient, city: str, name: PlaceInput, *, label: str = "地点"
    ) -> dict[str, str]:
        place = AmapPlace(name=name) if isinstance(name, str) else name
        target = place.name
        if place.entrance and not self._place_name(target).endswith(
            self._place_name(place.entrance)
        ):
            target += place.entrance
        # All explicit qualifiers are also verified locally; search ranking is not proof.
        keywords = " ".join(value for value in (place.district, place.address, target) if value)
        if len(keywords) > 80:
            raise MapServiceError(
                f"AMAP_QUERY_TOO_LONG: {label}检索条件超过80字符，请精简，保留地点全称与必要限定"
            )
        data = await self._get_json(
            client,
            "/v5/place/text",
            {
                "key": self._api_key,
                "keywords": keywords,
                "region": city,
                "city_limit": "true",
                "page_size": "25",
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
                and self._valid_location(item["location"])
            ]
            if isinstance(raw, list)
            else []
        )
        if not pois:
            raise MapServiceError(
                f"AMAP_POI_NOT_FOUND: {label}未找到有效地点，请核对城市、地点全称或入口"
            )
        # Dedupe only provider-identified, identical places; equal names alone are not identity.
        unique = []
        seen = set()
        for item in pois:
            identity = tuple(
                str(item.get(key, "")) for key in ("id", "name", "location", "adname", "address")
            )
            if item.get("id") and identity in seen:
                continue
            seen.add(identity)
            unique.append(item)
        ranked = [(self._poi_score(item, place, city), -index, item) for index, item in enumerate(unique)]
        ranked = [entry for entry in ranked if entry[0] is not None]
        if not ranked:
            raise MapServiceError(
                f"AMAP_POI_NO_MATCH: {label}候选未匹配地点全称或区县/地址/入口；"
                "请核对 name/district/address/entrance，不要猜测入口，也不要原样重试"
            )
        # User-selected policy: highest local match score, provider rank breaks ties.
        score, _, chosen = max(ranked, key=lambda entry: (entry[0], entry[1]))
        return {
            "name": chosen["name"],
            "location": chosen["location"],
            "id": str(chosen.get("id") or ""),
            "match_score": str(score),
        }

    @classmethod
    def _poi_score(cls, item: dict[str, Any], place: AmapPlace, city: str) -> float | None:
        """Local relevance score, NOT a provider score or a calibrated probability."""
        if not cls._matches_qualifiers(item, place, city):
            return None
        target = cls._local_name(place.name, city, place.district)
        if place.entrance and not target.endswith(cls._place_name(place.entrance)):
            target += cls._place_name(place.entrance)
        actual = cls._local_name(item["name"], city, place.district)
        # Do not turn a named attraction/entrance into a nearby transport or retail POI.
        for kind in ("地铁站", "公交", "停车场", "售票", "餐厅", "饭店", "酒店", "便利店", "商店"):
            if kind in actual and kind not in target:
                return None
        gate_pattern = r"(东北|东南|西北|西南|东|西|南|北)([一二三四五六七八九0-9]*)门"
        gate = re.search(gate_pattern, target)
        actual_gate = re.search(gate_pattern, actual)
        if gate:
            if not actual_gate or actual_gate.group(1) != gate.group(1):
                return None
            if gate.group(2) and actual_gate.group(2) != gate.group(2):
                return None
            base = target[:gate.start()]
            actual_base = actual[:actual_gate.start()]
            # Provider names can insert campus/site qualifiers before the gate.
            # Keep the complete requested site as an anchor; score the extra qualifier.
            if not base or not actual_base.startswith(base):
                return None
        elif actual_gate:
            # An unspecified entrance must not silently replace a requested whole site.
            return None
        address = item.get("address")
        address_exact = isinstance(address, str) and cls._local_name(address, city, place.district) == target
        if actual == target:
            score = 100.0
        elif gate:
            score = round(90 * SequenceMatcher(None, base, actual_base).ratio(), 2)
        elif address_exact:
            score = 90.0
        else:
            similarity = SequenceMatcher(None, target, actual).ratio()
            if similarity < 0.8 or len(target) < 4:
                return None
            score = round(80 * similarity, 2)
        return score + (10 if place.district else 0) + (10 if place.address else 0)

    @classmethod
    def _local_name(cls, value: str, city: str, district: str | None) -> str:
        value = cls._place_name(value)
        # Only strip caller-supplied administrative prefixes, not arbitrary place suffixes.
        # Strip explicit administrative suffixes only: 北京大学 must not become 大学.
        city_prefix = city if city.endswith("市") else f"{city}市"
        prefixes = [city_prefix, district or ""]
        for prefix in prefixes:
            normalized = cls._place_name(prefix)
            if normalized and value.startswith(normalized) and len(value) > len(normalized):
                value = value[len(normalized) :]
        return value

    @classmethod
    def _matches_qualifiers(cls, item: dict[str, Any], place: AmapPlace, city: str) -> bool:
        if place.district:
            district = item.get("adname")
            if not isinstance(district, str) or cls._place_name(district) != cls._place_name(
                place.district
            ):
                return False
        if place.address:
            address = item.get("address")
            if not isinstance(address, str):
                return False
            expected = cls._local_name(place.address, city, place.district)
            actual = cls._local_name(address, city, place.district)
            # Exact address comparison avoids matching 1号 against 11号/1号旁.
            if expected != actual:
                return False
        return True

    @staticmethod
    def _valid_location(value: str) -> bool:
        try:
            longitude, latitude = map(float, value.split(","))
            return (
                math.isfinite(longitude)
                and math.isfinite(latitude)
                and -180 <= longitude <= 180
                and -90 <= latitude <= 90
            )
        except ValueError:
            return False

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
            if (
                isinstance(data, dict)
                and str(data.get("infocode")) in {"10004", "10021"}
                and attempt < 2
            ):
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
