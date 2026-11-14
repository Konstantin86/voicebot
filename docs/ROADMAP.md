# Roadmap

Ordered by value. Each phase builds on the previous one.
Status: ✅ done · 🟡 in progress · ⬜ not started.

## Phase 1: Make it useful every day
1. ✅ **Memory across sessions.**
   - Short-term: the last 12 hours of the household's conversations (up to 80 sentences) are loaded into
     each new session, with who said each sentence.
   - Facts: "remember that…" / "forget…" tools; kept forever in `/var/lib/voicebot/memory/facts.json`
     (private per person unless shared).
   - ✅ Long-term: after each conversation, a short English summary (text-only Azure call, ~$0.001) is
     embedded on the Pi (multilingual-e5-small) and stored in the local Qdrant (`voicebot_memory`).
     `search_memory` is called only for questions about the past. Full text kept 30 days.
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
     once a film or video starts. Films and cartoons go to the disk first, YouTube only when asked.
   - ✅ TV web browser: web search or a website on the TV screen.
   - ✅ World clock: time and date anywhere, from the Pi's clock (no web search).
   - ✅ YouTube on the TV: video by description, by channel, or the channel's latest
     (YouTube Data API, ~90 searches a day); gets past YouTube's account screen.
   - ✅ OLX.ua: cheapest listings by city or all of Ukraine, price range, listing details and warning signs
     (website data feed; filters out accessories, boxes, buy ads and, for parts, whole PCs).
   - ✅ AUTO.RIA used cars (official API): listings with year, price, mileage, city, seller damage marks
     and listing checks; "more options", "without accidents"; 30-minute cache and hourly-limit handling.
   - ✅ Timers, reminders and alarms (shared by the household): set, list and cancel by voice; once, daily,
     weekdays or weekends. The spoken sentence is made when set (~$0.0005) and played by the Pi when due;
     alarms ring up to 3 min, timers 1 min, until a button or the wake word stops them. Said during a
     conversation if one is running.
   - ⬜ Calendar (Outlook or Google): "What's on tomorrow?"
4. ⬜ **Morning brief:** at a set time, speak the weather, today's calendar and headlines.

## Phase 2: Truly hands-free
5. ✅ **Wake word:** "Hey Jarvis" (openWakeWord) and "Привет/Привіт, Джарвис" (Vosk), all on the Pi CPU
   (about 9% of one core).
   The mic listens locally; nothing goes to Azure until the wake word. The call button stays as backup.
   - ✅ False wakes: the bot stays quiet when people talk among themselves (ignore_speech), hangs up after
     two such sentences, and ends after 10 s if nobody talks to it.
6. 🟡 **Who is speaking:** voiceprints per family member, computed on the Pi (sherpa-onnx + WeSpeaker).
   - ✅ "Remember my voice" (asks the name, ~10 s of speech), greeting by name, private facts per person,
     "forget my voice".
   - ✅ Each sentence judged on its own; long ones checked in two halves, so two voices in one sentence go
     to nobody; follows speaker changes. A voice is never saved under another person's name.
   - ⬜ Children's voices are close to each other and to their mother's; watch the scores.
   - ⬜ Per-person language, kids mode, calendar.
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
    - ✅ Long conversations no longer slow down (1.5 s → 6 s before): only the last 30 conversation items
      are kept.
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

## Backlog: new ideas
Status ⬜ unless marked. Numbers are for reference, not order.

### Living in Ukraine
- B1. ⬜ **Air raid alerts** (alerts.in.ua API, free token): announce start and end for Kharkiv region,
  answer "is there an alert now?", pause the TV when an alert starts.
- B2. ⬜ **Power outage schedule:** "when is the power off today?", a warning 15 minutes before an outage.
- B3. ⬜ **Exchange rates:** National Bank of Ukraine and bank rates by voice.

### The TV as the bot's screen
- B4. ⬜ **Cards on the TV:** the Pi serves small web pages (weather, timer countdown, shopping list,
  car comparison) and opens them in the TV browser.
- B5. ⬜ **Continue watching:** remember each person's last episode; "next episode" just works.
- B6. ⬜ **Sleep timer:** "turn off the TV in 30 minutes"; for kids, "cartoons for 40 minutes, then off".
- B7. ⬜ **Film night:** suggest films from the disk based on who is in the room and what they watched.

### Family and kids
- B8. ⬜ **Bedtime stories:** a new short story each night, in the child's language, with their name and toys.
- B9. ⬜ **Family quiz:** questions by voice, scoreboard on the TV, answers counted per person by voice.
- B10. ⬜ **Kids mode:** when a child is recognised: simpler answers, safe searches, TV time limit.
- B11. ⬜ **Language buddy:** English conversation practice with gentle corrections at the person's level.

### Around the house
- B12. ⬜ **Lights:** the WiZ bulb on the local network; "movie mode" dims the lights when a film starts.
- B13. ⬜ **Cooking mode:** step-by-step recipes hands-free, "next step", built-in timers.
- B14. ⬜ **Shared shopping list:** "add milk" by voice; read it in Telegram at the shop.

### Reminders and Telegram, bigger
- B15. ⬜ **Reminders for a person:** "remind Alina at 6 to call her mom"; said aloud when she is near,
  or sent to her Telegram.
- B16. ⬜ **Telegram:** intercom (type on the phone, the bot reads it at home), daily cost and usage
  report, ask the bot from outside with the same memory.

## Suggested order
Air raid alerts (B1) → Telegram (B15, B16) → TV sleep timer and continue watching (B5, B6)
→ morning brief (4) → deployment (12).

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
- Qdrant (klai_qdrant container) listens on all addresses with no API key; bind it to 127.0.0.1 or add a key.
- The small model sometimes fills gaps after an empty web search (e.g. a wrong flight route); accepted for now.
