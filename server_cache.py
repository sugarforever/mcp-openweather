import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, Dict
import requests
from datetime import datetime, timedelta
from mcp.server import Server
from mcp.server.lowlevel import NotificationOptions
from mcp.server import stdio
from mcp.types import Tool, Parameter, InitializationOptions


class CacheEntry:
    """缓存条目，包含数据和过期时间"""
    def __init__(self, data: Dict[str, Any], expire_at: datetime):
        self.data = data
        self.expire_at = expire_at


class WeatherServerContext:
    """天气服务器上下文，用于管理服务器状态"""
    def __init__(self, api_key: str):
        self.api_key = api_key
        # 服务器运行状态
        self.api_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        # 天气数据缓存，使用城市名作为键
        self.weather_cache: Dict[str, CacheEntry] = {}

    def get_cached_weather(self, city: str) -> Dict[str, Any] | None:
        """获取缓存的天气信息，如果已过期则返回 None"""
        if city not in self.weather_cache:
            self.cache_misses += 1
            return None
        
        entry = self.weather_cache[city]
        if entry.expire_at < datetime.now():
            self.cache_misses += 1
            del self.weather_cache[city]
            return None
        
        self.cache_hits += 1
        return entry.data

    def cache_weather(self, city: str, weather_data: Dict[str, Any]):
        """缓存天气信息，设置 5 分钟的过期时间"""
        expire_at = datetime.now() + timedelta(minutes=5)
        self.weather_cache[city] = CacheEntry(weather_data, expire_at)

    def get_weather_from_api(self, city: str) -> Dict[str, Any]:
        """从 OpenWeather API 获取天气数据"""
        self.api_calls += 1
        
        try:
            # 第一步，获取城市的地理位置
            geo_response = requests.get(
                "http://api.openweathermap.org/geo/1.0/direct",
                params={
                    "q": city,
                    "limit": 1,
                    "appid": self.api_key
                }
            )
            geo_response.raise_for_status()
            locations = geo_response.json()
            
            if not locations:
                return {"error": f"未找到城市的位置信息: {city}"}
                
            location = locations[0]
            
            # 第二步，基于地理位置，获取天气数据
            weather_response = requests.get(
                "https://api.openweathermap.org/data/3.0/onecall",
                params={
                    "lat": location["lat"],
                    "lon": location["lon"],
                    "units": "metric",
                    "exclude": "minutely,hourly,daily,alerts",
                    "appid": self.api_key
                }
            )
            weather_response.raise_for_status()
            data = weather_response.json()
            current = data["current"]

            formatted_response = {
                "city": location["name"],
                "country": location["country"],
                "state": location.get("state"),
                "coordinates": {
                    "lat": location["lat"],
                    "lon": location["lon"]
                },
                "temperature": {
                    "current": f"{current['temp']}°C",
                    "feels_like": f"{current['feels_like']}°C"
                },
                "weather": {
                    "main": current["weather"][0]["main"],
                    "description": current["weather"][0]["description"],
                    "icon": f"https://openweathermap.org/img/wn/{current['weather'][0]['icon']}@2x.png",
                },
                "details": {
                    "humidity": f"{current['humidity']}%",
                    "pressure": f"{current['pressure']} hPa",
                    "wind_speed": f"{current['wind_speed']} m/s",
                    "wind_direction": f"{current['wind_deg']}°",
                    "wind_gust": f"{current.get('wind_gust', 0)} m/s",
                    "cloudiness": f"{current['clouds']}%",
                    "uvi": current["uvi"],
                    "visibility": f"{current.get('visibility', 0)/1000:.1f} km"
                },
                "sun": {
                    "sunrise": datetime.fromtimestamp(current["sunrise"]).strftime("%H:%M:%S"),
                    "sunset": datetime.fromtimestamp(current["sunset"]).strftime("%H:%M:%S"),
                },
                "timezone": {
                    "name": data["timezone"],
                    "offset": data["timezone_offset"]
                },
                "timestamp": datetime.fromtimestamp(current["dt"]).isoformat()
            }

            return formatted_response
            
        except requests.exceptions.RequestException as e:
            error_message = f"OpenWeather API 错误: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    if 'message' in error_data:
                        error_message = f"OpenWeather API 错误信息: {error_data['message']}"
                except ValueError:
                    pass
            return {"error": error_message}


@asynccontextmanager
async def weather_lifespan(server: Server) -> AsyncIterator[WeatherServerContext]:
    """管理服务器生命周期"""
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        raise ValueError("OPENWEATHER_API_KEY 环境变量未设置")
    
    ctx = WeatherServerContext(api_key)
    try:
        yield ctx
    finally:
        # 服务器关闭时输出统计信息
        total_queries = ctx.cache_hits + ctx.cache_misses
        hit_rate = (ctx.cache_hits / total_queries * 100) if total_queries > 0 else 0
        print(f"服务器运行统计:")
        print(f"- API 调用次数: {ctx.api_calls}")
        print(f"- 缓存命中次数: {ctx.cache_hits}")
        print(f"- 缓存未命中次数: {ctx.cache_misses}")
        print(f"- 缓存命中率: {hit_rate:.1f}%")


class WeatherServer(Server):
    """天气预报服务器的主类"""
    def __init__(self):
        super().__init__("Weather", lifespan=weather_lifespan)
        self._setup_handlers()

    def _setup_handlers(self):
        """注册服务器的各种处理器"""
        self.register_list_tools_handler(self._handle_list_tools)
        self.register_call_tool_handler(self._handle_call_tool)

    async def _handle_list_tools(self) -> list[Tool]:
        """处理工具列表请求"""
        return [
            Tool(
                name="current_weather",
                description="Query the current weather by city name",
                parameters=[
                    Parameter(
                        name="city",
                        description="City name",
                        type="string",
                        required=True
                    )
                ]
            )
        ]

    async def _handle_call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """处理工具调用请求"""
        if name != "current_weather":
            raise ValueError(f"未知的工具: {name}")
        
        city = arguments.get("city")
        if not city:
            raise ValueError("缺少城市参数")

        # 获取服务器上下文
        ctx: WeatherServerContext = self.request_context.lifespan_context

        # 尝试从缓存获取天气数据
        weather_data = ctx.get_cached_weather(city)
        if weather_data is None:
            # 缓存未命中，调用API获取天气数据
            weather_data = ctx.get_weather_from_api(city)
            
            # 如果获取成功，缓存天气数据
            if "error" not in weather_data:
                ctx.cache_weather(city, weather_data)
        
        return weather_data


async def run_server():
    """启动并运行服务器"""
    server = WeatherServer()
    async with stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="weather",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_server()) 