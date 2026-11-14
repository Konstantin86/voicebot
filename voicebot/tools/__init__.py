"""Function-calling tools. Add a tool by creating a module and registering it in default_registry()."""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any | Awaitable[Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def __bool__(self) -> bool:
        return bool(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    async def call(self, name: str, arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            kwargs = json.loads(arguments) if arguments else {}
            result = tool.handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as e:
            log.exception("tool %s failed", name)
            return json.dumps({"error": str(e)})


    def names(self) -> list[str]:
        return list(self._tools)


def default_registry(memory=None, weather_home: str = "", weather_detect_home: bool = False,
                     voices=None, current=None) -> ToolRegistry:
    import os

    from . import auto_ria, memory_tools, olx, tv, weather, web_search, youtube

    registry = ToolRegistry()
    registry.register(memory_tools.end_session_tool())
    registry.register(weather.make_tool(weather_home, weather_detect_home))
    registry.register(olx.make_tool(weather_home))
    ria_tool = auto_ria.from_env()
    if ria_tool:
        registry.register(ria_tool)
    if memory is not None:
        for tool in memory_tools.make_tools(memory, current):
            registry.register(tool)
    if voices is not None and current is not None:
        for tool in memory_tools.voice_tools(voices, current):
            registry.register(tool)
    tools, tavily_video_finder = web_search.from_env()
    for tool in tools:
        registry.register(tool)
    if not tools:
        log.info("web_search and price_lookup disabled (no TAVILY_API_KEY)")
    youtube_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    find_video = youtube.make_video_finder(youtube_key) if youtube_key else tavily_video_finder
    tv_tool = tv.make_tool(
        os.environ.get("TV_SETTINGS", "/var/lib/voicebot/tv.json"),
        find_video,
        films_index=os.environ.get("TV_FILMS_INDEX", "/var/lib/voicebot/films.tsv"),
        media_root=os.environ.get("TV_MEDIA_ROOT", "/tmp/usb/sda/sda2"),
    )
    if tv_tool:
        registry.register(tv_tool)
        log.info("tv tool enabled; video search: %s",
                 "YouTube API" if youtube_key else "web search" if find_video else "none")
    return registry
