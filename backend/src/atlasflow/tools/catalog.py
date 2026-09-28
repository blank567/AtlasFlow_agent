"""Single registration entrypoint shared by the web backend and Studio."""

from atlasflow.config import Settings
from atlasflow.tools.base import BaseTool
from atlasflow.tools.calculator import CalculatorTool
from atlasflow.tools.map_itinerary import MapItineraryTool
from atlasflow.tools.map_route import AmapRequestLimiter, AmapRouteTool
from atlasflow.tools.poi_details import PoiDetailsTool
from atlasflow.tools.registry import ToolRegistry
from atlasflow.tools.weather_forecast import WeatherForecastTool
from atlasflow.tools.web_fetch import OpenRouterWebFetchTool
from atlasflow.tools.web_search import OpenRouterWebSearchTool


def build_tool_registry(settings: Settings, *, web_search: BaseTool | None) -> ToolRegistry:
    registry = ToolRegistry(
        timeout_seconds=settings.tool_timeout_seconds,
        max_retries=settings.max_tool_retries,
    )
    if web_search is not None:
        registry.register(web_search)
        if isinstance(web_search, OpenRouterWebSearchTool):
            registry.register(OpenRouterWebFetchTool(web_search.client, web_search.model))
    registry.register(CalculatorTool())
    amap_limiter = AmapRequestLimiter()
    registry.register(AmapRouteTool(settings.amap_api_key, request_limiter=amap_limiter))
    registry.register(MapItineraryTool(settings.amap_api_key, request_limiter=amap_limiter))
    registry.register(PoiDetailsTool(settings.amap_api_key, request_limiter=amap_limiter))
    registry.register(WeatherForecastTool(api_key=settings.open_meteo_api_key))
    return registry
