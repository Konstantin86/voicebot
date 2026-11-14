# Roadmap

Ordered by value. Each phase builds on the previous one.
Status: ✅ done · 🟡 in progress · ⬜ not started.

## Phase 1: Make it useful every day
1. ✅ **Memory across sessions.**
   - Short-term: the last 12 hours of conversations are loaded into each new session.
   - Long-term: "remember that…" / "forget…" tools; facts in `/var/lib/voicebot/memory/facts.json`.
2. ✅ **End the call by voice** in any of the three languages ("bye", "that's it", "пока", "бувай").
   The model calls `end_session`; a local goodbye check is the backup.
3. **Tools that answer real questions:**
   - ✅ Web search and news (Tavily `web_search` tool, 50/day limit). Searches before answering about
     recent changes, official facts that can change, or claims it disagrees with.
   - ✅ Product prices from Price Navigator (pn.com.ua): cheapest price, range, shops in Kharkiv.
   - ✅ Weather (Open-Meteo: free, no key). Home city from `WEATHER_HOME`, the `Location:` line in
     context.md, or detected from the internet address (IP).
   - ✅ LG TV (webOS 6, C1): on (Wake-on-LAN) / off, apps, inputs (PS5), volume, mute, pause/play.
   - ✅ Films from the TV's USB disk by voice ("play Moana"): film list from the disk, matching in three
     languages with near-word matching, asks when several fit; waits for the disk after power-on; hangs up
     once a film or video starts.
   - ✅ TV web browser: web search or a website on the TV screen.
   - ✅ YouTube on the TV: video by description, by channel, or the channel's latest
     (YouTube Data API, ~90 searches a day); gets past YouTube's account screen.
   - ✅ OLX.ua: cheapest listings by city or all of Ukraine, price range, listing details and warning signs
     (website data feed; filters out accessories, boxes, buy ads and, for parts, whole PCs).
   - ✅ AUTO.RIA used cars (official API): listings with year, price, mileage, city, seller damage marks
     and listing checks; "more options", "without accidents"; 30-minute cache and hourly-limit handling.
   - ⬜ Timers, alarms, reminders. Needs the bot to speak unprompted: play a sound, or open a short
     Azure session to say the reminder.
   - ⬜ Calendar (Outlook or Google): "What's on tomorrow?"
4. ⬜ **Morning brief:** at a set time, speak the weather, today's calendar and headlines.

## Phase 2: Truly hands-free
5. ✅ **Wake word:** "Hey Jarvis" (openWakeWord) and "Привет/Привіт, Джарвис" (Vosk), all on the Pi CPU
   (about 9% of one core).
   The mic listens locally; nothing goes to Azure until the wake word. The call button stays as backup.
6. ⬜ **Who is speaking:** voice fingerprints per family member, computed on the Pi.
   The bot knows who asked: name, calendar, language, kids mode.
7. ⬜ **Telegram link:** conversation summaries on the phone; change voice, model or budget from the phone;
   send a message from the phone to be read aloud at home (intercom).
8. ⬜ **More of the home:** Wi-Fi lights and plugs (a WiZ bulb and an ESP device are already on the
   network), scenes like "movie mode" (TV on, lights down).
9. ⬜ **Music and radio:** "play jazz radio". Mix the stream into the speaker output the bot already owns,
   and lower the music while someone speaks.

## Phase 3: Quality and reliability
10. 🟡 **Measure latency and quality:** time from end of speech to first answer audio, how often the user
    interrupts, wrong-language rate. Use the numbers to compare `gpt-realtime-mini`, `gpt-realtime`,
    `gpt-realtime-2.1-mini`, transcription models and turn-detection settings.
    - ✅ The log shows when each reply starts and when its first audio arrives (about 2 s with a tool call),
      and warns when Azure marks a reply as failed.
11. 🟡 **Better Ukrainian and Russian:**
    - ✅ Reply language follows the user's last sentence: replies start after the transcript, with a
      per-turn language rule; memory no longer pulls replies into Ukrainian.
    - ✅ No more Belarusian transcripts: MAI Transcribe (no Belarusian) replaces gpt-4o-mini-transcribe;
      transcripts also arrive faster (replies 0.1–0.4 s after speech).
    - ✅ `mai-transcribe` (now the default; see above).
    - ✅ OpenAI voices compared by ear with the dot button; marin chosen.
    - ✅ Azure HD voices tried in all three languages; the English ones (Ava, Andrew2, Emma2, Ava Omni) sound
      best and are in `VOICES`.
12. ⬜ **Deployment you can trust:**
    - `make deploy`: copy code, restart, check it started (sudo rule limited to that one restart).
    - GitHub Actions running the tests on every push.
    - systemd watchdog that restarts the bot if it hangs.
    - Install script that also sets up a fresh Ubuntu 26.04, so the OS fix is one command.
13. ⬜ **Budget guard:** if today's estimate passes a limit, say "daily budget reached", switch to mini
    or stop, and send a Telegram message.

## Phase 4: Local brain on the RTX 4070 PC
14. ⬜ **Local pipeline as a second brain:** faster-whisper + Ollama (8–14B) + Piper, possibly via Pipecat.
    The PC is woken over the network from the Pi (Wake-on-LAN).
    - Smart routing: simple questions local (zero cost), hard ones to Azure.
    - Fallback: if the internet or Azure is down, the local brain answers.
    - The code is ready for this: `voicebot/brains/` is a plug-in folder.

## Phase 5: The robot body
15. ⬜ **Two GC9A01 round displays as eyes** (1.28-inch), driven by the bot's states:
    idle, listening, thinking, speaking. Blinking, looking toward the speaker,
    mouth or eye glow following speech loudness. Voice Live visemes for lip sync with Azure voices.
16. ⬜ **Enclosure:** a box or toy with the Jabra's top uncovered, a status LED,
    and an optional physical mute switch.

## Suggested order
Timers and reminders (3) → morning brief (4) → deployment (12) → more of the home (8).

## Done so far
- ✅ Version 1: call/hang-up, Voice Live conversation, barge-in, 30 s silence timeout,
  context + date/time, systemd service.
- ✅ Jabra off-hook light confirmation, volume keys.
- ✅ Dot button: single press = next voice, double press = switch model (spoken confirmation).
- ✅ Token totals and cost estimate per session and per day in the log.
- ✅ Web search (Tavily) with daily limit, searching sound and reply-language hint.
- ✅ Price Navigator (pn.com.ua) prices, single-site search, cheapest offers.
- ✅ Memory: remember/forget facts, recent conversations (12 h); end the call by saying goodbye.
- ✅ Weather tool; strict shopping prices (only from cheapest_offers / price_lookup).
- ✅ Home city from IP for weather; searches before answering about recent changes.
- ✅ Wake word (English, Russian, Ukrainian); replies start after the transcript, with a per-turn language rule.
- ✅ LG TV control (webOS): on/off (Wake-on-LAN), apps, inputs, volume, pause/play; YouTube videos by
  search, channel or "latest" (YouTube Data API), passing YouTube's account screen.

- ✅ Dot button between conversations: plays the next voice saying its name, no session started.
- ✅ OLX and AUTO.RIA search; MAI Transcribe; better Russian/Ukrainian detection.

## Still open from earlier
- Speech style: answer first, no repeated question, no stock "let me know" endings; keep checking by ear.
- Azure sometimes stalls mid-session; a watchdog now logs Azure's last events and retries once. Watch the logs.
- OS: reinstall Ubuntu 26.04 LTS later (see `docs/NEXT_STEPS.md`).
- Remove the leftover wake word test files in `/tmp` on the Pi.
- Film list: rebuild with deploy/index_films.sh when films are added (disk on the Pi for a moment).
