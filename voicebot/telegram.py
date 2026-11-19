"""Telegram link: the family's phones and the bot.

- From a phone: a text message is read aloud at home (intercom); /today, /voice, /model, /budget.
- To a phone: "send me this to Telegram" by voice, the evening digest, the daily limit warning.

Only listed chats may use it: the owner (TELEGRAM_OWNER_ID) and the people the owner adds with /allow.
It uses the Telegram Bot API over HTTPS with long polling: no web server, no open port, no extra package.
"""

import asyncio
import datetime
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .links import LINKS

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
POLL_SECONDS = 50
RETRY_SECONDS = (5, 15, 60)
MAX_TEXT = 4000  # Telegram allows 4096 characters per message
MAX_SPOKEN = 400
INTERCOM_MAX_AGE = 600  # seconds; older messages (the Pi was off) are not read aloud
FILE_MODE = 0o640

HELP = """\
🏠 Home voice assistant: quick guide

TALKING TO IT AT HOME
• Say "Привет, Джарвис" (or "Hey Jarvis"), or press the call button on the Jabra. Wait for the beep, then speak.
• Speak Russian, Ukrainian or English: it answers in the language you used.
• It ends by itself after a short silence, or say "пока" / "бувай" / "bye", or press the button again.
• The dot button: one press = next voice, two presses = basic or smart model.

THINGS TO ASK
• Timers, reminders, alarms: "таймер на 10 минут", "напомни в 18:00 позвонить маме", "разбуди меня в 7 по будням".
  Stop a ringing alarm with the wake word or any Jabra button.
• Weather, time in other countries, news and facts, prices in shops, cars on AUTO.RIA, listings on OLX.
• TV: "включи мультик на YouTube", "громче", "пауза", "включи диск".
• Memory: "запомни, что…", "что ты обо мне знаешь?", "что мы решили про диван?".
• Voices: "запомни мой голос, я Алина" (once per person), so it knows who is speaking.
• Telegram: "скинь мне это в телеграм": links, model names, any info goes to your phone.

TELEGRAM
• Send any text: it is read aloud at home.
• /today: today's conversations and cost.
• /reminders: next and past timers, reminders and alarms; tap one to remove it.
• /help: this guide.
Owner only:
• /voice, /model: change the bot's voice or model (buttons).
• /wake: which wake words it listens for, or off (call button only).
• /budget 2: daily spending limit in dollars (0 = none); at the limit the basic model is used until midnight.
• /allow <id> <name>: let someone use this bot (name as their voice is saved). They get their id by messaging the bot.
• /remove <id or name>, /people: who can use it."""

COMMANDS = [("help", "Quick guide"), ("today", "Today's conversations and cost"),
            ("reminders", "Next and past reminders")]
OWNER_COMMANDS = COMMANDS + [("voice", "Change the voice"), ("model", "Basic or smart model"),
                             ("wake", "Wake word on or off"),
                             ("budget", "Daily spending limit"), ("people", "Who can use the bot"),
                             ("allow", "Add someone: /allow <id> <name>"), ("remove", "Remove someone")]
WAKE_CHOICES = [("all", "Both: Привет Джарвис + Hey Jarvis"), ("russian", "Only Привет / Привіт Джарвис"),
                ("off", "Off: call button only")]
REPEAT_WORDS = {"daily": "every day", "weekdays": "on weekdays", "weekends": "at weekends"}
PAST_SHOWN = 10


class TelegramError(Exception):
    pass


class Api:
    """The Bot API over HTTPS. The token is part of every address, so addresses are never logged."""

    def __init__(self, token: str):
        self._token = token

    def __repr__(self) -> str:
        return "Api(<token hidden>)"

    def call(self, method: str, **params: Any) -> Any:
        request = urllib.request.Request(
            API.format(token=self._token, method=method), data=json.dumps(params).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=params.get("timeout", 0) + 15) as response:
                body = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                description = json.loads(e.read() or b"{}").get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"{method}: HTTP {e.code} {description}".strip()) from None
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(f"{method}: {getattr(e, 'reason', e)}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'failed')}")
        return body.get("result")


LATIN = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
                 ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                  "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"]))


def _plain(name: str) -> str:
    """'Костя' and 'Kostya' compare equal: lowercase Latin letters only."""
    latin = "".join(LATIN.get(c, c) for c in name.strip().lower())
    return "".join(c for c in latin if c.isalpha()).replace("kh", "h").replace("ya", "ia")


class Chats:
    """Who may use the bot: Telegram chat id -> the person's name (as their voice is saved)."""

    def __init__(self, path: str, owner: int):
        self.path = path
        self.owner = owner
        self.people: dict[int, str] = {}
        self.digest_day = ""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.people = {int(k): v for k, v in data.get("people", {}).items()}
            self.digest_day = data.get("digest_day", "")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("telegram: cannot read %s: %s", path, e)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"people": {str(k): v for k, v in self.people.items()}, "digest_day": self.digest_day},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def allowed(self, chat: int) -> bool:
        return bool(chat) and (chat == self.owner or chat in self.people)

    def name_of(self, chat: int) -> str | None:
        return self.people.get(chat)

    def chat_of(self, name: str | None) -> int | None:
        if not name:
            return None
        wanted = _plain(name)
        for chat, who in self.people.items():
            if _plain(who) == wanted:
                return chat
        return None

    def allow(self, chat: int, name: str) -> None:
        self.people[chat] = name.strip()
        self.save()

    def remove(self, key: str) -> str | None:
        chat = int(key) if key.lstrip("-").isdigit() else self.chat_of(key)
        if chat is None or chat not in self.people:
            return None
        name = self.people.pop(chat)
        self.save()
        return name


@dataclass
class Home:
    """What the Telegram side may see and change at home."""

    settings: Any  # RuntimeSettings
    announce: Callable[[str, str], Awaitable[None]]  # (text, sender) -> read aloud at home
    cost_today: Callable[[], float]
    summaries: Callable[[datetime.date], list[dict]]
    voice_name: Callable[[str], str]
    known_names: Callable[[], list[str]]
    basic_model: str
    over_budget: Callable[[], bool]
    reminders: Any = None  # reminders.Reminders
    apply_wake: Callable[[str], None] = lambda mode: None


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text] if text else parts


def digest_text(day: datetime.date, summaries: list[dict], cost: float, budget: float) -> str:
    if summaries:
        lines = [f"{day:%A %d %B}: {len(summaries)} conversation{'s' if len(summaries) != 1 else ''} worth keeping."]
        for r in summaries:
            people = ", ".join(r.get("people") or []) or "someone"
            lines.append(f"• {str(r.get('date', ''))[11:16]} ({people}): {r.get('summary', '')}")
    else:
        lines = [f"{day:%A %d %B}: nothing worth keeping today."]
    lines.append(f"Cost today: ${cost:.2f}" + (f" (limit ${budget:.2f})" if budget else ""))
    return "\n".join(lines)


def _reminder_line(kind: str, due: float, text: str, repeat: str = "", missed: bool = False) -> str:
    when = datetime.datetime.fromtimestamp(due).strftime("%a %d %b %H:%M")
    extra = f", {REPEAT_WORDS[repeat]}" if repeat in REPEAT_WORDS else ""
    return f"• {when}, {kind}{extra}: {text}" + (" (missed: the bot was off)" if missed else "")


def reminders_text(store) -> str:
    if store is None:
        return "Reminders are not available."
    upcoming = store.upcoming()
    lines = ["⏰ Next:"] + ([_reminder_line(r.kind, r.due, r.text, r.repeat) for r in upcoming] or ["• nothing set"])
    past = store.history[-PAST_SHOWN:][::-1]
    lines += ["", "🕓 Past:"] + ([_reminder_line(h["kind"], h["due"], h["text"], h.get("repeat", ""),
                                                 h.get("missed", False)) for h in past] or ["• none yet"])
    return "\n".join(lines)


def reminder_buttons(store) -> list[list[dict]] | None:
    """One 'remove' button per upcoming reminder."""
    if store is None:
        return None
    rows = []
    for r in store.upcoming():
        when = datetime.datetime.fromtimestamp(r.due).strftime("%a %H:%M")
        rows.append([{"text": f"✖ {when} {r.label or r.text}"[:60], "callback_data": f"cancel:{r.id}"}])
    return rows or None


def model_label(model: str, basic: str) -> str:
    if model == basic:
        return "Basic: "
    return "Live (full duplex): " if model.startswith("gpt-live") else "Smart: "


def parse_dollars(text: str) -> float:
    value = float(text.strip().lstrip("$").rstrip("$").replace(",", ".").strip())
    if value < 0:
        raise ValueError
    return value


class Bot:
    def __init__(self, api: Api, chats: Chats, home: Home, digest_time: str = ""):
        self.api = api
        self.chats = chats
        self.home = home
        self.digest_time = digest_time
        self._asked: set[int] = set()
        self._speaking = asyncio.Lock()
        self._stopped = threading.Event()
        self._background: set[asyncio.Task] = set()

    # Receiving: a thread waits on Telegram (long polling) and hands each update to the event loop.

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        threading.Thread(target=self._poll, args=(loop,), name="telegram", daemon=True).start()
        log.info("telegram: on (owner %s, %d more people)", self.chats.owner or "not set", len(self.chats.people))

    def stop(self) -> None:
        self._stopped.set()

    def _set_menu(self) -> None:
        """The command list Telegram offers after typing '/'; the owner's chat also gets the owner commands."""
        def listed(commands):
            return [{"command": c, "description": d} for c, d in commands]
        try:
            self.api.call("setMyCommands", commands=listed(COMMANDS))
            if self.chats.owner:
                self.api.call("setMyCommands", commands=listed(OWNER_COMMANDS),
                              scope={"type": "chat", "chat_id": self.chats.owner})
        except TelegramError as e:
            log.warning("telegram: could not set the command menu: %s", e)

    def _poll(self, loop: asyncio.AbstractEventLoop) -> None:
        self._set_menu()
        offset = None
        failures = 0
        while not self._stopped.is_set():
            params: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = self.api.call("getUpdates", **params)
            except TelegramError as e:
                if failures == 0 or failures % 20 == 0:
                    log.warning("telegram: %s", e)
                self._stopped.wait(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1
                continue
            if failures:
                log.info("telegram: connected again")
            failures = 0
            for update in updates or []:
                offset = update["update_id"] + 1
                asyncio.run_coroutine_threadsafe(self.handle(update), loop)

    async def handle(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self._on_button(update["callback_query"])
            elif "message" in update:
                await self._on_message(update["message"])
        except Exception:
            log.exception("telegram: could not handle an update")

    # Sending

    async def send(self, chat: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        parts = split_text(text)
        for i, part in enumerate(parts):
            params: dict[str, Any] = {"chat_id": chat, "text": part}
            if buttons and i == len(parts) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}
            await asyncio.to_thread(self.api.call, "sendMessage", **params)

    async def tell_owner(self, text: str) -> None:
        if not self.chats.owner:
            log.info("telegram: no owner set; not sent: %s", text)
            return
        try:
            await self.send(self.chats.owner, text)
        except TelegramError as e:
            log.warning("telegram: could not message the owner: %s", e)

    # Messages from phones

    async def _on_message(self, msg: dict) -> None:
        chat_info = msg.get("chat") or {}
        if chat_info.get("type") != "private":
            return
        chat = chat_info.get("id")
        text = (msg.get("text") or "").strip()
        if not self.chats.allowed(chat):
            await self._stranger(chat, msg.get("from") or {})
            return
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            await self._command(chat, command[1:].split("@")[0].lower(), arg.strip())
        elif text:
            await self._intercom(chat, msg, text)
        else:
            await self.send(chat, "Only text messages can be read aloud for now.")

    async def _stranger(self, chat: int, user: dict) -> None:
        if chat in self._asked:
            return
        self._asked.add(chat)
        who = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Someone"
        if user.get("username"):
            who += f" (@{user['username']})"
        log.info("telegram: message from a chat not on the list: %s, id %s", who, chat)
        await self.send(chat, f"This is a private home assistant. Your Telegram id is {chat}. "
                              "Ask the owner to add you.")
        if self.chats.owner:
            await self.tell_owner(f"{who}, id {chat}, wants to use the bot.\n"
                                  f"To allow: /allow {chat} <name as their voice is saved>")

    async def _intercom(self, chat: int, msg: dict, text: str) -> None:
        if time.time() - msg.get("date", time.time()) > INTERCOM_MAX_AGE:
            await self.send(chat, "That message arrived while the bot was offline, so it was not read aloud.")
            return
        if len(text) > MAX_SPOKEN:
            await self.send(chat, f"Too long to read aloud: please keep it under {MAX_SPOKEN} characters.")
            return
        sender = self.chats.name_of(chat) or ((msg.get("from") or {}).get("first_name")) or "Someone"
        log.info("telegram: intercom from %s: %s", sender, text)
        try:
            async with self._speaking:
                await self.home.announce(text, sender)
        except Exception as e:
            log.warning("telegram: could not read the message aloud: %s", e)
            await self.send(chat, "Could not read it aloud at home.")
            return
        await self.send(chat, "🔊 Read aloud at home.")

    async def _command(self, chat: int, command: str, arg: str) -> None:
        if command in ("start", "help"):
            await self.send(chat, HELP + (f"\nYour Telegram id is {chat}." if chat == self.chats.owner else ""))
            return
        if command == "today":
            await self.send(chat, self.today_text())
            return
        if command == "reminders":
            buttons = reminder_buttons(self.home.reminders)
            await self.send(chat, reminders_text(self.home.reminders) + ("\n\nTap one to remove it." if buttons else ""),
                            buttons)
            return
        if chat != self.chats.owner:
            await self.send(chat, "Only the owner can do that.")
            return
        settings = self.home.settings
        if command == "voice":
            buttons = [{"text": ("✓ " if v == settings.voice else "") + self.home.voice_name(v), "callback_data": f"voice:{i}"}
                       for i, v in enumerate(settings.voices)]
            await self.send(chat, f"Pick a voice for {settings.model}:", [buttons[i:i + 2] for i in range(0, len(buttons), 2)])
        elif command == "model":
            buttons = [[{"text": ("✓ " if m == settings.model else "") + model_label(m, self.home.basic_model) + m,
                         "callback_data": f"model:{i}"}] for i, m in enumerate(settings.models)]
            await self.send(chat, "Pick a model:", buttons)
        elif command == "wake":
            buttons = [[{"text": ("✓ " if mode == settings.wake else "") + label, "callback_data": f"wake:{i}"}]
                       for i, (mode, label) in enumerate(WAKE_CHOICES)]
            await self.send(chat, "Wake word:", buttons)
        elif command == "budget":
            if not arg:
                limit = f"${settings.budget:.2f} a day" if settings.budget else "no limit"
                await self.send(chat, f"Daily limit: {limit}. Spent today: ${self.home.cost_today():.2f}.\n"
                                      "Change it with /budget 2 (0 = no limit).")
                return
            try:
                settings.set_budget(parse_dollars(arg))
            except ValueError:
                await self.send(chat, "Give the limit in dollars, for example /budget 2 or /budget 0.5.")
                return
            log.info("telegram: daily limit set to $%.2f", settings.budget)
            await self.send(chat, f"Daily limit set to ${settings.budget:.2f}." if settings.budget
                            else "Daily limit removed.")
        elif command == "allow":
            key, _, name = arg.partition(" ")
            if not key.lstrip("-").isdigit() or not name.strip():
                await self.send(chat, "Use: /allow <id> <name>, for example /allow 123456789 Алина")
                return
            self.chats.allow(int(key), name)
            note = "" if any(_plain(n) == _plain(name) for n in self.home.known_names()) else \
                f"\nNote: no voice is saved as {name.strip()} yet; 'send me' works once the bot knows that voice."
            await self.send(chat, f"{name.strip()} can use the bot now.{note}")
        elif command == "remove":
            name = self.chats.remove(arg) if arg else None
            await self.send(chat, f"{name} removed." if name else "Nobody like that on the list.")
        elif command == "people":
            lines = [f"{who}: {cid}" for cid, who in self.chats.people.items()]
            await self.send(chat, "\n".join(lines) or "Only you so far. Add people with /allow <id> <name>.")
        else:
            await self.send(chat, HELP)

    async def _on_button(self, query: dict) -> None:
        chat = ((query.get("message") or {}).get("chat") or {}).get("id")
        message_id = (query.get("message") or {}).get("message_id")
        kind, _, index = (query.get("data") or "").partition(":")
        settings = self.home.settings
        if kind == "cancel" and self.chats.allowed(chat) and index.isdigit() and self.home.reminders is not None:
            item = self.home.reminders.cancel(int(index))
            who = self.chats.name_of(chat) or "the owner"
            if item:
                log.info("telegram: %s removed %s #%d: %s", who, item.kind, item.id, item.text)
            note = f"Removed: {item.text}" if item else "Already gone."
            await asyncio.to_thread(self.api.call, "answerCallbackQuery", callback_query_id=query.get("id"), text=note)
            buttons = reminder_buttons(self.home.reminders)
            params: dict[str, Any] = {"chat_id": chat, "message_id": message_id,
                                      "text": reminders_text(self.home.reminders)}
            if buttons:
                params["reply_markup"] = {"inline_keyboard": buttons}
            await asyncio.to_thread(self.api.call, "editMessageText", **params)
            return
        answer = "Only the owner can do that."
        if chat == self.chats.owner and index.isdigit():
            if kind == "voice" and int(index) < len(settings.voices):
                settings.set_voice(settings.voices[int(index)])
                answer = f"Voice: {self.home.voice_name(settings.voice)}. Used from the next conversation."
            elif kind == "wake" and int(index) < len(WAKE_CHOICES):
                mode, label = WAKE_CHOICES[int(index)]
                settings.set_wake(mode)
                self.home.apply_wake(mode)
                answer = f"Wake word: {label}."
            elif kind == "model" and int(index) < len(settings.models):
                settings.set_model(settings.models[int(index)])
                answer = (f"Model: {settings.model}, voice {self.home.voice_name(settings.voice)}. "
                          "Used from the next conversation. /voice lists this model's voices.")
                if settings.model != self.home.basic_model and self.home.over_budget():
                    answer += " Today's limit is reached, so the basic model is used until midnight."
            log.info("telegram: %s", answer)
        await asyncio.to_thread(self.api.call, "answerCallbackQuery", callback_query_id=query.get("id"))
        if chat and message_id:
            await asyncio.to_thread(self.api.call, "editMessageText", chat_id=chat, message_id=message_id, text=answer)

    # The evening digest

    def today_text(self) -> str:
        return digest_text(datetime.date.today(), self.home.summaries(datetime.date.today()),
                           self.home.cost_today(), self.home.settings.budget)

    async def digest_loop(self) -> None:
        if not self.digest_time or not self.chats.owner:
            return
        while not self._stopped.is_set():
            await asyncio.sleep(30)
            now = datetime.datetime.now()
            if now.strftime("%H:%M") < self.digest_time or self.chats.digest_day == now.date().isoformat():
                continue
            self.chats.digest_day = now.date().isoformat()
            try:
                self.chats.save()
                await self.tell_owner(self.today_text())
                log.info("telegram: sent the evening digest")
            except Exception:
                log.exception("telegram: could not send the evening digest")


def send_tool(bot: Bot, current=None):
    from .tools import Tool

    async def send_to_telegram(text: str = "", links: list[str] | None = None, to: str = "") -> dict:
        lines = [text.strip()]
        missing = []
        for ref in links or []:
            found = LINKS.get(str(ref))
            if found:
                url, title = found
                lines.append(f"{title}\n{url}" if title else url)
            else:
                missing.append(str(ref))
        message = "\n\n".join(line for line in lines if line)
        if not message:
            return {"error": "Nothing to send: give text or links."}
        note = ""
        if to.strip():
            name, chat = to.strip(), bot.chats.chat_of(to)
            if chat is None:
                linked = ", ".join(bot.chats.people.values()) or "nobody"
                return {"error": f"{name} has no Telegram linked to the bot. Linked: {linked}."}
        else:
            speaker = current.speaker if current else None
            chat = bot.chats.chat_of(speaker)
            name = speaker
            if chat is None:
                chat, name = bot.chats.owner, bot.chats.name_of(bot.chats.owner) or "the owner"
                if speaker:
                    note = f" {speaker} has no Telegram linked, so it went to {name}."
        if not chat:
            return {"error": "Telegram is not set up yet (no owner chat)."}
        try:
            await bot.send(chat, message)
        except TelegramError as e:
            log.warning("telegram: send_to_telegram failed: %s", e)
            return {"error": "Telegram did not accept the message. Tell the user it was not sent."}
        log.info("telegram: sent to %s: %s", name, message.replace("\n", " | ")[:300])
        result = {"status": "sent", "to": name,
                  "note": "Say in a few words that it was sent. Do not read links aloud." + note}
        if missing:
            result["missing_links"] = missing
        return result

    return Tool(
        name="send_to_telegram",
        description=(
            "Send a message to a family member's Telegram (their phone). Use it when asked to send, share or "
            "'drop' something to Telegram ('send me this', 'скинь в телеграм', 'надішли в телеграм'): links, "
            "model names, prices, addresses, lists, any info. Without 'to' it goes to the person speaking."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message, written to be read on a phone: the useful "
                         "facts (names, models, prices, addresses, short lists), in the user's language. "
                         "Never write web addresses yourself."},
                "links": {"type": "array", "items": {"type": "string"},
                          "description": "The 'link' values (like 'L3') of things found by tools that the user wants; "
                          "the real web links are added to the message."},
                "to": {"type": "string", "description": "Only when the user names someone else: their name."},
            },
        },
        handler=send_to_telegram,
    )


def build(token: str, owner: int, path: str, home: Home, digest_time: str = "") -> Bot | None:
    if not token:
        return None
    if not owner:
        log.warning("telegram: TELEGRAM_OWNER_ID is not set; send the bot a message and its reply shows your id")
    return Bot(Api(token), Chats(path, owner), home, digest_time)
