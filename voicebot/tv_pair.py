"""One-time pairing with the LG TV: python -m voicebot.tv_pair <tv-ip> <tv-mac> [settings-file]

Accept the prompt on the TV with the remote. The key is saved to the settings file, never printed.
"""

import asyncio
import sys

from .tools.tv import save_settings

ATTEMPTS = 6


async def pair(host: str, mac: str, path: str) -> int:
    from aiowebostv import WebOsClient

    for attempt in range(1, ATTEMPTS + 1):
        print(f"Attempt {attempt}/{ATTEMPTS}: press OK on the TV prompt within 10 seconds…", flush=True)
        client = WebOsClient(host)
        try:
            await client.connect()
        except Exception as e:
            print(f"  not paired yet ({type(e).__name__}: {e})")
            continue
        try:
            save_settings(path, {"host": host, "mac": mac, "client_key": client.client_key})
            apps = sorted(a.get("title", "") for a in client.tv_state.apps.values())
            inputs = sorted(i.get("label", "") for i in client.tv_state.inputs.values())
            print(f"Paired. Saved to {path}.")
            print("Apps:", ", ".join(apps))
            print("Inputs:", ", ".join(inputs))
            return 0
        finally:
            await client.disconnect()
    print("Pairing failed. Is the TV on and showing the prompt?")
    return 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = sys.argv[3] if len(sys.argv) > 3 else "/var/lib/voicebot/tv.json"
    return asyncio.run(pair(sys.argv[1], sys.argv[2], path))


if __name__ == "__main__":
    sys.exit(main())
