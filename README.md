# voicebot

Hands-free home voice assistant. Raspberry Pi 5 + Jabra Speak2 40 are the ears and mouth;
Azure Foundry Voice Live (`gpt-realtime-mini`) is the brain.

- Call button → session starts (rising beep). Speak in English, Ukrainian or Russian.
- Hang-up button, or 30 s of silence → session ends (falling beep). Low double beep = error, see logs.
- Volume buttons change the Jabra volume.
- Say goodbye in any language ("bye", "пока", "бувай") and the bot hangs up after its reply.
- "Remember that…" / "запам'ятай…" saves a fact; "forget…" deletes it. Recent conversations
  (last 12 h) are remembered across hang-ups. Stored in `/var/lib/voicebot/memory/`.
- Weather for home (the `Location:` line in context.md) or any named city, via Open-Meteo.
- Dot button, only while no call is active: single press = next voice (it says its name),
  double press = switch between gpt-realtime-mini (one low beep) and gpt-realtime (two high beeps).

Plans: [docs/ROADMAP.md](docs/ROADMAP.md).

## Layout
| Path | What |
| --- | --- |
| `voicebot/app.py` | Main loop: buttons, sessions, silence timeout |
| `voicebot/audio.py` | Jabra audio through `aplay`/`arecord`; playback always open |
| `voicebot/jabra.py` | Call/hang-up (hidraw) and volume keys (evdev) |
| `voicebot/brains/` | `voicelive.py` = Azure. Add a local brain as another module here |
| `voicebot/tools/` | Function-calling tools (none yet) |
| `voicebot/prompt.py` | System prompt + `context.md` + date/time |
| `deploy/` | systemd unit, udev rule, install script |

## Install on the Pi
Set the Pi address once in your shell, for example `export PI=user@192.168.0.10`.

1. Create the app folder (once):
   ```bash
   ssh -t $PI 'sudo install -d -o $(id -un) -g $(id -gn) /opt/voicebot'
   ```
2. Copy the code from the Mac (also use this for every update):
   ```bash
   rsync -av --delete --exclude venv --exclude .git --exclude '__pycache__' ./ $PI:/opt/voicebot/
   ```
3. On the Pi, run the installer (creates the `voicebot` user, udev rule, venv, service):
   ```bash
   cd /opt/voicebot && sudo ./deploy/install.sh
   ```
4. Fill in the Azure endpoint and key (see `docs/AZURE_SETUP.md`):
   ```bash
   sudoedit /etc/voicebot/voicebot.env
   ```
5. Edit household info: `/etc/voicebot/context.md`.
6. Start and watch logs:
   ```bash
   sudo systemctl restart voicebot && journalctl -u voicebot -f
   ```

Start a session without the button: `sudo systemctl kill -s USR1 voicebot`.

## Tests (Mac or Pi)
```bash
python3 -m pytest -q tests
```
