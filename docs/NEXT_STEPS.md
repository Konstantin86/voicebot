# Next steps

## OS fix (deferred)
The bot is developed and tested on the current Pi system (Ubuntu 25.10, kernel 6.17 via tryboot).
Fix the OS later. Options:
- Fresh install of Ubuntu Server 26.04 LTS on a new SD card (recommended). Back up first:
  avtoskop Postgres (pg_dump), avtoskop_photos volume, qdrant data, compose folders + .env,
  crontab, UFW rules, ~/.ssh.
- Or in-place upgrade to 26.04 LTS.

Until then:
- Do NOT run `apt upgrade`: sources point to `devel` (unstable next release).
- 25.10 is end of life (no security updates since July 2026).
- A normal reboot returns to kernel 6.5. Either make 6.17 permanent or retest the Jabra on 6.5.

## Later stages
- End the session when the user says goodbye (an `end_session` tool the model can call).
- If gpt-realtime-mini mixes up facts too often, try gpt-realtime or gpt-realtime-2.1-mini (VOICELIVE_MODEL).
- Tools: calendar, weather, web search, Home Assistant; RAG via Azure AI Search.
- Local brain on the RTX 4070 Super PC (faster-whisper + Ollama/llama.cpp + Piper), Wake-on-LAN.
- Jabra mute button: report 02 bit 0x04 is a mute press; set the mute light (output report 02 bit 0x02).
- Tool: web search / news, so the bot can answer questions about recent events.
- Robot body with two GC9A01 round displays as eyes.

## Jabra HID notes (verified 2026-11-13)
- Input report 02, byte 1: bit 0x01 hook switch, 0x02 line busy, 0x04 mute press, 0x08 flash press.
- Output report 02, byte 1: bit 0x01 off-hook light, 0x02 mute light, 0x04 ring, 0x08 hold.
- Call press sets the hook bit. The computer must confirm with output 02 01 00 at once,
  or the Jabra drops the call by itself. Each press flickers the hook bit for ~15 ms.
- Hang-up clears the hook bit. Turning the light off (02 00 00) also releases the call.
- Dot button = KEY_PLAYPAUSE (consumer report 01 08 00). The Jabra sends it only while no call is
  active; during a call (off-hook light on) presses are not sent to the computer at all.
  No long-press event: press and release arrive together.
