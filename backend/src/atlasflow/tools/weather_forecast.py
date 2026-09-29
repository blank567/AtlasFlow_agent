"""Attributable, report-safe short-range weather forecasts from Open-Meteo."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from atlasflow.schemas import Evidence
from atlasflow.tools.base import BaseTool, Capability, RiskLevel, ToolContext, ToolResult


class WeatherForecastArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    city: str = Field(min_length=2, max_length=100, description="城市名称；重名时附国家或省份")
    days: int = Field(default=4, ge=1, le=7, description="从今天起的预测天数，最多 7 天")


class WeatherForecastTool(BaseTool):
    name = "weather_forecast"
    description = (
        "查询指定城市未来 1–7 天的天气预报、气温和降水概率。"
        "结果来自 Open-Meteo，适用于非商业演示；报告需注明预测查询时间和来源。"
    )
    risk_level = RiskLevel.LOW
    arguments_model = WeatherForecastArguments
    capabilities = (Capability("weather_forecast", "获取可注明来源及查询时间的短期天气预报", True),)
    allowed_agents = ("researcher", "critic", "quality_gate")
    _country_aliases: ClassVar[dict[str, str]] = {"中国": "CN", "china": "CN", "cn": "CN"}

    def __init__(
        self,
        *,
        api_key: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._transport = transport

    async def run(self, arguments: WeatherForecastArguments, context: ToolContext) -> ToolResult:
        paid = bool(self._api_key)
        city_parts = [part.strip() for part in arguments.city.replace("，", ",").split(",") if part.strip()]
        city_name = city_parts[0].removesuffix("市")
        geocode_host = (
            "https://customer-geocoding-api.open-meteo.com"
            if paid else "https://geocoding-api.open-meteo.com"
        )
        forecast_host = (
            "https://customer-api.open-meteo.com" if paid else "https://api.open-meteo.com"
        )
        geocode_params: dict[str, str | int] = {
            "name": city_name,
            "count": 10,
            "language": "zh" if any("\u4e00" <= char <= "\u9fff" for char in city_name) else "en",
            "format": "json",
        }
        for qualifier in city_parts[1:]:
            country_code = self._country_aliases.get(qualifier.casefold())
            if country_code:
                geocode_params["countryCode"] = country_code
                break
        if paid:
            geocode_params["apikey"] = self._api_key
        try:
            async with httpx.AsyncClient(timeout=12, transport=self._transport) as client:
                lookup = await client.get(f"{geocode_host}/v1/search", params=geocode_params)
                lookup.raise_for_status()
                locations = lookup.json().get("results", [])
                if not isinstance(locations, list) or not locations:
                    return ToolResult(success=False, error="WEATHER_CITY_NOT_FOUND: 未找到城市")
                place = self._choose_location(locations, arguments.city)
                if place is None:
                    return ToolResult(
                        success=False,
                        error="WEATHER_CITY_AMBIGUOUS: 城市有多个候选，请补充国家或省份",
                    )
                latitude = float(place["latitude"])
                longitude = float(place["longitude"])
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    raise ValueError("invalid coordinates")
                params: dict[str, str | float | int] = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max"
                    ),
                    "timezone": "auto",
                    "forecast_days": arguments.days,
                }
                if paid:
                    params["apikey"] = self._api_key
                forecast = await client.get(f"{forecast_host}/v1/forecast", params=params)
                forecast.raise_for_status()
                payload = forecast.json()
                daily = payload.get("daily") if isinstance(payload, dict) else None
                if not isinstance(daily, dict):
                    raise TypeError("missing daily data")
                dates = daily.get("time")
                highs = daily.get("temperature_2m_max")
                lows = daily.get("temperature_2m_min")
                rain = daily.get("precipitation_probability_max")
                codes = daily.get("weather_code")
                columns = (dates, highs, lows, rain, codes)
                if not all(isinstance(col, list) and len(col) == arguments.days for col in columns):
                    raise ValueError("incomplete daily data")
                rows = [
                    {
                        "date": str(dates[index]),
                        "temperature_max_c": float(highs[index]),
                        "temperature_min_c": float(lows[index]),
                        "precipitation_probability_max_pct": int(rain[index]),
                        "weather_code": int(codes[index]),
                    }
                    for index in range(arguments.days)
                ]
        except httpx.TimeoutException:
            return ToolResult(success=False, error="WEATHER_TIMEOUT: 天气服务超时", retryable=True)
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                success=False,
                error=f"WEATHER_HTTP_{exc.response.status_code}: 天气服务请求失败",
            )
        except httpx.HTTPError:
            return ToolResult(success=False, error="WEATHER_NETWORK: 无法连接天气服务", retryable=True)
        except (TypeError, ValueError, KeyError, AttributeError):
            return ToolResult(success=False, error="WEATHER_INVALID_RESPONSE: 天气数据格式异常")

        retrieved_at = datetime.now(UTC).isoformat()
        place_label = ", ".join(
            str(part) for part in (place.get("name"), place.get("admin1"), place.get("country")) if part
        )
        lines = [
            (
                f"{row['date']}: {row['temperature_min_c']:g}–{row['temperature_max_c']:g}°C, "
                f"最高降水概率 {row['precipitation_probability_max_pct']}%, "
                f"WMO 天气代码 {row['weather_code']}"
            )
            for row in rows
        ]
        summary = f"{place_label} 天气预报（查询于 {retrieved_at} UTC）：" + "；".join(lines)
        # Source links must never contain a paid API credential. The public API
        # uses the same query syntax and is an independently checkable citation.
        source_url = "https://api.open-meteo.com/v1/forecast?" + urlencode(
            {key: value for key, value in params.items() if key != "apikey"}
        )
        evidence = Evidence(
            source_id=f"open-meteo:{latitude},{longitude}:{retrieved_at}",
            title=f"Open-Meteo 天气预报：{place_label}",
            content=summary + "。数据署名：Open-Meteo / GeoNames；天气为预测，并非实测或保证。",
            uri=source_url,
            score=1.0,
            metadata={
                "provider": "open-meteo",
                "data_kind": "forecast",
                "retrieved_at": retrieved_at,
                "license": "CC BY 4.0; API free tier non-commercial only",
                "attribution": "Open-Meteo / GeoNames",
            },
        )
        return ToolResult(
            success=True,
            data={
                "provider": "open-meteo",
                "location": place_label,
                "timezone": str(payload.get("timezone") or ""),
                "retrieved_at": retrieved_at,
                "forecast": rows,
                "summary": summary,
                "source_url": source_url,
                "attribution": "Open-Meteo / GeoNames",
            },
            evidence=[evidence],
        )

    @staticmethod
    def _choose_location(locations: list[Any], query: str) -> dict[str, Any] | None:
        candidates = [
            row
            for row in locations
            if isinstance(row, dict) and row.get("latitude") is not None and row.get("longitude") is not None
        ]
        parts = [part.strip() for part in query.replace("，", ",").split(",") if part.strip()]
        if not parts:
            return None
        for qualifier in parts[1:]:
            normalized = WeatherForecastTool._normalize_name(qualifier)
            country_code = WeatherForecastTool._country_aliases.get(normalized)
            candidates = [
                row
                for row in candidates
                if (
                    (country_code is not None and str(row.get("country_code") or "").upper() == country_code)
                    or normalized in {
                        WeatherForecastTool._normalize_name(str(row.get(field) or ""))
                        for field in ("admin1", "admin2", "admin3", "country", "country_code")
                    }
                )
            ]
        if not candidates:
            return None
        name = WeatherForecastTool._normalize_name(parts[0])
        exact = [
            row
            for row in candidates
            if WeatherForecastTool._normalize_name(str(row.get("name") or "")) == name
        ]
        candidates = exact or candidates
        if len(candidates) == 1:
            return candidates[0]
        # A uniquely identified national capital resolves common localized-name
        # duplicates such as 北京 / Beijing without selecting an arbitrary first hit.
        capitals = [row for row in candidates if row.get("feature_code") == "PPLC"]
        if len(capitals) == 1:
            return capitals[0]
        # For other duplicate names, select only a clearly dominant large city.
        ranked = sorted(candidates, key=lambda row: int(row.get("population") or 0), reverse=True)
        if (
            len(ranked) >= 2
            and int(ranked[0].get("population") or 0) >= 1_000_000
            and int(ranked[0].get("population") or 0) >= 5 * max(1, int(ranked[1].get("population") or 0))
        ):
            return ranked[0]
        return None

    @staticmethod
    def _normalize_name(value: str) -> str:
        return value.strip().casefold().removesuffix("市")
