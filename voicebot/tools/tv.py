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
from urllib.parse import quote_plus
from typing import Any

from . import Tool, films

log = logging.getLogger(__name__)

YOUTUBE_APP = "youtube.leanback.v4"
WAKE_SECONDS = 20
YOUTUBE_START_SECONDS = 6
YOUTUBE_PROFILE_SECONDS = 2
VOLUME_STEP_DEFAULT = 3
MEDIA_PLAYER_APP = "com.webos.app.mediadiscovery"
BROWSER_APP = "com.webos.app.browser"
SEARCH_URL = "https://www.google.com/search?q="
SURE_SCORE = 0.9  # play the best film match right away when it is this good and clearly ahead
CLEAR_LEAD = 0.13
DISK_READY_SECONDS = 25  # after the bot turns the TV on, its USB disk needs time before files open
WATCHING_NOTE = ("It is playing now. Confirm in a few words and then call end_session, "
                 "so the TV's sound is not taken for the user's speech.")

ACTIONS = ["on", "off", "open_app", "input", "volume_up", "volume_down", "set_volume", "mute", "unmute",
           "pause", "play", "youtube", "play_film", "browse", "status"]

DESCRIPTION = (
    "Control the living room LG TV: turn on/off, open an app (YouTube, Netflix…), switch input "
    "(e.g. PS5, HDMI 2), volume, mute, pause/play, play a YouTube video found by a search query, "
    "play a film, cartoon or series episode from the hard disk attached to the TV, through its media player "
    "(play_film; 'с диска', 'на диске', 'з диска', 'медиаплеер' all mean this), "
    "or open the TV's web browser with a web search or a website (browse)."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "app": {"type": "string", "description": "App name for open_app, in English as shown on the TV, e.g. 'YouTube', 'Netflix' (also when the user says 'Ютуб')."},
        "input": {"type": "string", "description": "Input for input, in English, e.g. 'PS5', 'HDMI 2'. Only when the user names an input or device; "
                  "never for the disk (that is play_film)."},
        "level": {"type": "integer", "description": "Volume 0-100 for set_volume."},
        "steps": {"type": "integer", "description": "Steps for volume_up / volume_down (default 3)."},
        "query": {"type": "string", "description": "For youtube: what the video is about or its title, e.g. 'Peppa Pig camping'. Can be empty when channel is set."},
        "channel": {"type": "string", "description": "For youtube: the channel name when the user names one, e.g. 'MrBeast'."},
        "latest": {"type": "boolean", "description": "For youtube: true for the newest / latest video."},
        "title": {"type": "string", "description": "For play_film: the film's title in English (international title), e.g. 'Frozen' when the user says 'Холодное сердце'."},
        "original_title": {"type": "string", "description": "For play_film: the title as the user said it, e.g. 'Крижане серце'."},
        "russian_title": {"type": "string", "description": "For play_film: the film's Russian title (some files are named in Russian), e.g. 'Холодное сердце'."},
        "year": {"type": "integer", "description": "For play_film: release year, only when the user names it or chose from options."},
        "file": {"type": "string", "description": "For play_film: an exact file from earlier play_film options, when the user picked one."},
        "search": {"type": "string", "description": "For browse: what to search for on the web, in the user's language, e.g. 'нові мультфільми 2026'."},
        "url": {"type": "string", "description": "For browse: a website to open instead of a search, e.g. 'https://multiplex.ua'."},
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
    def __init__(self, settings_path: str, find_video: Callable[[str], Awaitable[dict]] | None = None,
                 films_index: str = "", media_root: str = ""):
        self.path = settings_path
        self.find_video = find_video
        self.films_index = films_index
        self.media_root = media_root.rstrip("/")
        self.turned_on_at = 0.0
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
                self.turned_on_at = loop.time()
                return {"result": "The TV is on."}
        return {"error": "The TV did not turn on."}

    async def _do(self, client, action: str, app: str = "", input: str = "", level: int | None = None,
                  steps: int | None = None, query: str = "", channel: str = "", latest: bool = False,
                  title: str = "", original_title: str = "", russian_title: str = "", year: int | None = None,
                  file: str = "", search: str = "", url: str = "") -> dict[str, Any]:
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
            return {"result": "Playing on YouTube.", "note": WATCHING_NOTE,
                    **{k: v for k, v in video.items() if k != "id"}}
        if action == "browse":
            return await self._browse(client, search, url)
        if action == "play_film":
            return await self._play_film(client, [title, original_title, russian_title], year, file)
        return {"error": f"unknown action {action}"}

    async def _browse(self, client, search: str, url: str) -> dict[str, Any]:
        if url and not re.match(r"^https?://", url):
            url = "https://" + url
        if not url:
            if not search:
                return {"error": "search or url is required"}
            url = SEARCH_URL + quote_plus(search)
        try:
            await client.request("system.launcher/open", {"target": url})
        except Exception as e:
            log.info("tv: launcher open failed (%s); trying the browser app", e)
            await client.launch_app_with_params(BROWSER_APP, {"target": url})
        log.info("tv: browser %s", url)
        return {"result": "Opened in the TV's browser.", "note": "Confirm in a few words. Do not read the address."}

    async def _play_film(self, client, titles: list[str], year: int | None, file: str) -> dict[str, Any]:
        library = films.load(self.films_index) if self.films_index else []
        if not library:
            return {"error": "There is no film list for the TV's disk yet."}
        if file:
            chosen = next((f for f in library if f.path == file), None)
            if not chosen:
                return {"error": "That file is not in the film list."}
        else:
            titles = [t for t in titles if t]
            matches = films.find(library, titles, year)
            if not matches:
                log.info("tv: no film for %s", " / ".join(titles))
                return {"result": "No such film on the disk.", "note": "Say so; do not offer YouTube unless asked."}
            best_score, chosen = matches[0]
            close = [f for s, f in matches if s >= best_score - CLEAR_LEAD]
            if best_score < SURE_SCORE or len(close) > 1:
                log.info("tv: film options for %s: %s", " / ".join(titles), ", ".join(f.path for _, f in matches))
                return {
                    "result": "Several films match. Ask the user which one (by title and year); then call "
                              "play_film again with file set to the chosen one.",
                    "options": [{"file": f.path, "title": f.title, "year": f.year} for _, f in matches],
                }
        wait = DISK_READY_SECONDS - (asyncio.get_running_loop().time() - self.turned_on_at)
        if self.turned_on_at and wait > 0:
            log.info("tv: waiting %.0f s for the disk after turning the TV on", wait)
            await asyncio.sleep(wait)
        path = f"{self.media_root}/{chosen.path}"
        await client.launch_app_with_params(MEDIA_PLAYER_APP, {"payload": [{
            "fullPath": path, "fileName": os.path.basename(path), "mediaType": "VIDEO", "deviceType": "usb",
        }]})
        log.info("tv: playing %s", chosen.path)
        return {"result": "Playing on the TV.", "title": chosen.title, "year": chosen.year,
                "note": WATCHING_NOTE + " If the user later says the TV cannot find the file, the disk may not be "
                        "connected to the TV."}


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


def make_tool(settings_path: str, find_video: Callable[[str], Awaitable[dict]] | None = None,
              films_index: str = "", media_root: str = "/tmp/usb/sda/sda2") -> Tool | None:
    if not load_settings(settings_path):
        return None
    tv = TV(settings_path, find_video, films_index, media_root)
    return Tool(name="tv", description=DESCRIPTION, parameters=PARAMETERS, handler=tv.run)
