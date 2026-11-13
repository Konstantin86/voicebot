import argparse
import asyncio
import logging
import os
import sys

from .config import Config, ConfigError, load_env_file

EX_CONFIG = 78


def main() -> int:
    parser = argparse.ArgumentParser(prog="voicebot")
    parser.add_argument("--env-file", help="file with KEY=VALUE settings")
    parser.add_argument("--start-now", action="store_true", help="start a session without pressing the call button")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    under_systemd = "INVOCATION_ID" in os.environ
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s" if under_systemd else "%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)

    if args.env_file:
        load_env_file(args.env_file)
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        logging.error("config: %s", e)
        return EX_CONFIG

    from .app import App

    asyncio.run(App(cfg).run(start_now=args.start_now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
