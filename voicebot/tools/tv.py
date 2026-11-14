"""tv tool: controls an LG webOS TV on the local network (aiowebostv).

Settings live in tv.json (host, mac, client_key), written once by `python -m voicebot.tv_pair`.
"""

import asyncio
import json
import logging
import os
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from . import Tool

log = logging.getLogger(__name__)

YOUTUBE_APP = "youtube.leanback.v4"
WAKE_SECONDS = 20
YOUTUBE_START_SECONDS = 6
YOUTUBE_PROFILE_SECONDS = 2
VOLUME_STEP_DEFAULT = 3

ACTIONS = ["on", "off", "open_app", "input", "volume_up", "volume_down", "set_volume", "mute", "unmute",
           "pause", "play", "youtube", "status"]

DESCRIPTION = (
    "Control the living room LG TV: turn on/off, open an app (YouTube, Netflix…), switch input "
    "(e.g. PS5, HDMI 2), volume, mute, pause/play, or play a YouTube video found by a search query."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "app": {"type": "string", "description": "App name for open_app, in English as shown on the TV, e.g. 'YouTube', 'Netflix' (also when the user says 'Ютуб')."},
        "input": {"type": "string", "description": "Input for input, in English, e.g. 'PS5', 'HDMI 2'."},
        "level": {"type": "integer", "description": "Volume 0-100 for set_volume."},
        "steps": {"type": "integer", "description": "Steps for volume_up / volume_down (default 3)."},
        "query": {"type": "string", "description": "For youtube: what the video is about or its title, e.g. 'Peppa Pig camping'. Can be empty when channel is set."},
        "channel": {"type": "string", "description": "For youtube: the channel name when the user names one, e.g. 'MrBeast'."},
        "latest": {"type": "boolean", "description": "For youtube: true for the newest / latest video."},
    },
    "required": ["action"],
}


def load_settings(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("tv: cannot read %s: %s", path, e)
        return None
    return data if data.get("host") else None


def save_settings(path: str, data: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def send_wake_on_lan(mac: str, host: str) -> None:
    raw = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac))
    if len(raw) != 6:
        raise ValueError(f"bad MAC address {mac!r}")
    packet = b"\xff" * 6 + raw * 16
    subnet_broadcast = ".".join(host.split(".")[:3] + ["255"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in ("255.255.255.255", subnet_broadcast):
            for port in (9, 7):
                s.sendto(packet, (target, port))


def find_by_name(name: str, items: dict[str, str]) -> str | None:
    """items maps id -> display name; matches exact, then contained, then by digits ('hdmi 2')."""
    wanted = re.sub(r"\s+", " ", name.lower()).strip()
    for item_id, title in items.items():
        if title.lower() == wanted:
            return item_id
    for item_id, title in items.items():
        if wanted in title.lower() or title.lower() in wanted:
            return item_id
    compact = wanted.replace(" ", "")
    for item_id, title in items.items():
        if compact and (compact in title.lower().replace(" ", "") or compact in item_id.lower()):
            return item_id
    return None


class TV:
    def __init__(self, settings_path: str, find_video: Callable[[str], Awaitable[dict]] | None = None):
        self.path = settings_path
        self.find_video = find_video
        self._lock = asyncio.Lock()

    async def _connect(self, settings: dict[str, Any]):
        from aiowebostv import WebOsClient

        client = WebOsClient(settings["host"], settings.get("client_key"))
        await client.connect()
        if client.client_key and client.client_key != settings.get("client_key"):
            settings["client_key"] = client.client_key
            save_settings(self.path, settings)
        return client

    async def _try_connect(self, settings: dict[str, Any]):
        try:
            return await self._connect(settings)
        except Exception as e:
            log.info("tv: not reachable (%s)", type(e).__name__)
            return None

    async def run(self, action: str, **kw: Any) -> dict[str, Any]:
        settings = load_settings(self.path)
        if not settings:
            return {"error": "The TV is not set up yet."}
        async with self._lock:
            if action == "on":
                return await self._power_on(settings)
            client = await self._try_connect(settings)
            if client is None:
                return {"result": "The TV is off." if action in ("off", "status") else
                        "The TV is off or not reachable. Turn it on first."}
            try:
                return await self._do(client, action, **kw)
            finally:
                await client.disconnect()

    async def _is_on(self, settings: dict[str, Any]) -> bool:
        client = await self._try_connect(settings)
        if client is None:
            return False
        try:
            return client.tv_state.is_on
        finally:
            await client.disconnect()

    async def _power_on(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Wake-on-LAN turns the TV on both from deep standby and from Quick Start+ standby
        (the C1 has no network "power on" command)."""
        if await self._is_on(settings):
            return {"result": "The TV is already on."}
        if not settings.get("mac"):
            return {"error": "The TV is off and no MAC address is set for waking it."}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAKE_SECONDS
        while loop.time() < deadline:
            send_wake_on_lan(settings["mac"], settings["host"])
            await asyncio.sleep(2)
            if await self._is_on(settings):
                log.info("tv: on after %.0f s", WAKE_SECONDS - (deadline - loop.time()))
                return {"result": "The TV is on."}
        return {"error": "The TV did not turn on."}

    async def _do(self, client, action: str, app: str = "", input: str = "", level: int | None = None,
                  steps: int | None = None, query: str = "", channel: str = "", latest: bool = False) -> dict[str, Any]:
        state = client.tv_state
        if action == "status":
            return {"on": state.is_on, "app": state.current_app_id, "volume": state.volume, "muted": state.muted}
        if not state.is_on:
            return {"result": "The TV is off." if action == "off" else "The TV is off. Turn it on first."}
        if action == "off":
            await client.power_off()
            return {"result": "The TV is turning off."}
        if action == "open_app":
            apps = {a: info.get("title", a) for a, info in state.apps.items()}
            app_id = find_by_name(app, apps)
            if not app_id:
                return {"error": f"No app called {app!r}.", "installed_apps": sorted(apps.values())}
            await client.launch_app(app_id)
            return {"result": f"Opened {apps[app_id]}."}
        if action == "input":
            inputs = {i: info.get("label", i) for i, info in state.inputs.items()}
            input_id = find_by_name(input, inputs)
            if not input_id:
                return {"error": f"No input called {input!r}.", "inputs": sorted(inputs.values())}
            await client.launch_app(input_id)
            return {"result": f"Switched to {inputs[input_id]}."}
        if action in ("volume_up", "volume_down"):
            for _ in range(max(1, min(steps or VOLUME_STEP_DEFAULT, 20))):
                await (client.volume_up() if action == "volume_up" else client.volume_down())
            return {"result": "Done."}
        if action == "set_volume":
            if level is None:
                return {"error": "level is required"}
            await client.set_volume(max(0, min(int(level), 100)))
            return {"result": f"Volume set to {level}."}
        if action in ("mute", "unmute"):
            await client.set_mute(action == "mute")
            return {"result": "Done."}
        if action == "pause":
            await client.pause()
            return {"result": "Paused."}
        if action == "play":
            await client.play()
            return {"result": "Playing."}
        if action == "youtube":
            if not self.find_video:
                await client.launch_app(YOUTUBE_APP)
                return {"result": "Opened YouTube. Searching for videos is not available."}
            video = await self.find_video(query=query, channel=channel, latest=bool(latest))
            if not video.get("id"):
                await client.launch_app(YOUTUBE_APP)
                return {"result": "Opened YouTube, but no matching video was found.", **video}
            await self._play_youtube(client, video["id"])
            log.info("tv: youtube %s (%s)", video["id"], video.get("title"))
            return {"result": "Playing on YouTube.", **{k: v for k, v in video.items() if k != "id"}}
        return {"error": f"unknown action {action}"}


    async def _play_youtube(self, client, video_id: str) -> None:
        """A freshly started YouTube shows "Who's watching?"; a video sent then is lost.
        So start it, accept the highlighted (last used) account, then send the video."""
        if client.tv_state.current_app_id != YOUTUBE_APP:
            await client.launch_app(YOUTUBE_APP)
            await asyncio.sleep(YOUTUBE_START_SECONDS)
            await client.button("ENTER")
            await asyncio.sleep(YOUTUBE_PROFILE_SECONDS)
        await client.launch_app_with_params(
            YOUTUBE_APP, {"contentTarget": f"https://www.youtube.com/tv?v={video_id}"}
        )


def make_tool(settings_path: str, find_video: Callable[[str], Awaitable[dict]] | None = None) -> Tool | None:
    if not load_settings(settings_path):
        return None
    tv = TV(settings_path, find_video)
    return Tool(name="tv", description=DESCRIPTION, parameters=PARAMETERS, handler=tv.run)
