"""Checks which AUTO.RIA API parts this key may use (3 requests; skips the paid average-price API). Prints status only, never the key.
Run as the bot: sudo -u voicebot bash -c 'set -a; . /etc/voicebot/voicebot.env; /opt/voicebot/venv/bin/python /opt/voicebot/deploy/check_auto_ria.py'
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("AUTO_RIA_API_KEY", "").strip()
BASE = "https://developers.ria.com/auto"


def call(name, path, params):
    url = f"{BASE}{path}?" + urllib.parse.urlencode([("api_key", KEY)] + params)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.loads(r.read() or b"null")
            print(f"{name:14} OK")
            return body
    except urllib.error.HTTPError as e:
        print(f"{name:14} HTTP {e.code}: {e.read()[:150].decode('utf-8', 'replace').replace(KEY, '***')}")
    except Exception as e:
        print(f"{name:14} failed: {type(e).__name__}")
    return None


if not KEY:
    raise SystemExit("AUTO_RIA_API_KEY is not set")
call("makes", "/categories/1/marks", [])
found = call("search", "/search", [("category_id", 1), ("countpage", 1)])
ids = ((found or {}).get("result") or {}).get("search_result", {}).get("ids") or []
if ids:
    call("listing info", "/info", [("auto_id", ids[0])])
