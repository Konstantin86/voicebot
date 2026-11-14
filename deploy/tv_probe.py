"""Read-only look at the TV: media apps and what it says about USB storage.

Run as the bot (the pairing key is only readable by it):
  sudo -u voicebot /opt/voicebot/venv/bin/python /opt/voicebot/deploy/tv_probe.py
Prints no keys. With --open <path on the disk> it tries four ways to make the Media Player play that file,
8 seconds apart (this changes the screen).
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiowebostv import WebOsClient  # noqa: E402

from voicebot.tools.tv import load_settings  # noqa: E402

SETTINGS = os.environ.get("TV_SETTINGS", "/var/lib/voicebot/tv.json")
STORAGE_CALLS = [
    "com.webos.service.pdm/getAttachedStorageDeviceList",
    "com.webos.service.pdm/getAttachedAllDeviceList",
    "com.webos.service.attachedstoragemanager/listDevices",
    "com.webos.service.mediaindexer/getDeviceList",
    "com.webos.service.mediaindexer/getMediaDbPermission",
]


def short(value, limit=700) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


async def main() -> None:
    settings = load_settings(SETTINGS)
    client = WebOsClient(settings["host"], settings.get("client_key"))
    await client.connect()
    try:
        if len(sys.argv) > 2 and sys.argv[1] == "--open":
            relative = sys.argv[2].lstrip("/")
            name = os.path.basename(relative)
            for number, (root, style) in enumerate(
                [("sda/sda1", "plain"), ("sda/sda1", "full"), ("sda/sda2", "plain"), ("sda/sda2", "full")], 1
            ):
                path = f"/tmp/usb/{root}/{relative}"
                item = {"fullPath": path, "fileName": name, "mediaType": "VIDEO", "deviceType": "usb"}
                if style == "full":
                    item = {"fullPath": f"file://{path}", "fileName": name, "title": name, "mediaType": "VIDEO",
                            "deviceType": "usb", "lastPlayPosition": -1, "thumbnail": "", "subtitle": "",
                            "artist": "", "album": ""}
                print(f"Trying {number}: {root}, {style} …", flush=True)
                try:
                    await client.launch_app_with_params("com.webos.app.mediadiscovery", {"payload": [item]})
                except Exception as e:
                    print("   failed:", type(e).__name__, e)
                await asyncio.sleep(8)
            return
        print("TV on:", client.tv_state.is_on, "| current app:", client.tv_state.current_app_id)
        apps = await client.get_apps_all()
        media = [(a.get("id"), a.get("title")) for a in apps
                 if any(w in (a.get("id", "") + a.get("title", "")).lower()
                        for w in ("photo", "video", "media", "usb", "music", "player"))]
        print("media apps:", media)
        for uri in STORAGE_CALLS:
            try:
                print(uri, "->", short(await client.request(uri, {})))
            except Exception as e:
                print(uri, "failed:", type(e).__name__, str(e)[:150])
    finally:
        await client.disconnect()


asyncio.run(main())
