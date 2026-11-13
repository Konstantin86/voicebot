# Roadmap

Ordered by value. Each phase builds on the previous one.
Status: ✅ done · 🟡 in progress · ⬜ not started.

## Phase 1: Make it useful every day
1. ⬜ **Memory across sessions.** Today every hang-up or voice change starts from zero.
   - Short-term: after each session, save a short summary; load the last few into the next session.
   - Long-term: "remember that…" / "forget…" as tools; facts stored in a file next to `context.md`.
2. ⬜ **End the call by voice.** "Bye" / "that's all" → the bot hangs up (`end_session` tool).
3. **Tools that answer real questions:**
   - ✅ Web search and news (Tavily `web_search` tool, 50/day limit, language hint from the transcript).
   - ⬜ Weather (Open-Meteo: free, no key).
   - ⬜ Timers, alarms, reminders. Needs the bot to speak unprompted: play a sound, or open a short
     Azure session to say the reminder.
   - ⬜ Calendar (Outlook or Google): "What's on tomorrow?"
4. ⬜ **Morning brief:** at a set time, speak the weather, today's calendar and headlines.

## Phase 2: Truly hands-free
5. ⬜ **Wake word** ("Hey Jarvis" or a custom name) with openWakeWord on the Pi 5 CPU.
   The mic listens locally; nothing goes to Azure until the wake word. The call button stays as backup.
6. ⬜ **Who is speaking:** voice fingerprints per family member, computed on the Pi.
   The bot knows who asked: name, calendar, language, kids mode.
7. ⬜ **Telegram link:** conversation summaries on the phone; change voice, model or budget from the phone;
   send a message from the phone to be read aloud at home (intercom).
8. ⬜ **Music and radio:** "play jazz radio". Mix the stream into the speaker output the bot already owns,
   and lower the music while someone speaks.

## Phase 3: Quality and reliability
9. ⬜ **Measure latency and quality:** time from end of speech to first answer audio, how often the user
   interrupts, wrong-language rate. Use the numbers to compare `gpt-realtime-mini`, `gpt-realtime`,
   `gpt-realtime-2.1-mini`, transcription models and turn-detection settings.
10. ⬜ **Better Ukrainian:** try Azure multilingual / HD voices and `mai-transcribe`; compare by ear.
11. ⬜ **Deployment you can trust:**
    - `make deploy`: copy code, restart, check it started (sudo rule limited to that one restart).
    - GitHub Actions running the tests on every push.
    - systemd watchdog that restarts the bot if it hangs.
    - Install script that also sets up a fresh Ubuntu 26.04, so the OS fix is one command.
12. ⬜ **Budget guard:** if today's estimate passes a limit, say "daily budget reached", switch to mini
    or stop, and send a Telegram message.

## Phase 4: Local brain on the RTX 4070 PC
13. ⬜ **Local pipeline as a second brain:** faster-whisper + Ollama (8–14B) + Piper, possibly via Pipecat.
    The PC is woken over the network from the Pi (Wake-on-LAN).
    - Smart routing: simple questions local (zero cost), hard ones to Azure.
    - Fallback: if the internet or Azure is down, the local brain answers.
    - The code is ready for this: `voicebot/brains/` is a plug-in folder.

## Phase 5: The robot body
14. ⬜ **Two GC9A01 round displays as eyes** (1.28-inch), driven by the bot's states:
    idle, listening, thinking, speaking. Blinking, looking toward the speaker,
    mouth or eye glow following speech loudness. Voice Live visemes for lip sync with Azure voices.
15. ⬜ **Enclosure:** a box or toy with the Jabra's top uncovered, a status LED,
    and an optional physical mute switch.

## Suggested order
Memory + `end_session` (1, 2) → weather and timers (3) → wake word (5).

## Done so far
- ✅ Version 1: call/hang-up, Voice Live conversation, barge-in, 30 s silence timeout,
  context + date/time, systemd service.
- ✅ Jabra off-hook light confirmation, volume keys.
- ✅ Dot button: single press = next voice, double press = switch model (spoken confirmation).
- ✅ Token totals and cost estimate per session and per day in the log.
- ✅ Web search (Tavily) with daily limit, searching sound and reply-language hint.
