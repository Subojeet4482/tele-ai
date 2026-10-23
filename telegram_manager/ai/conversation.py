"""Private chats the AI holds by itself, from one of the admin's accounts, toward one person.

The admin starts one ("Rahul se @user ko hello bolke chat karo, usko X batao"). From then on every
private message that person sends to that account is answered by the AI: straight away (auto) or as
a draft the admin approves (draft). The admin is told about every exchange and can stop it at any
time. A chat ends when the person says stop, the reply limit is used up, the AI finishes the goal,
or the admin ends it. When the AI cannot or should not answer (rude message, a question it cannot
answer) it pauses and asks the admin."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
import time
from contextlib import suppress
from typing import Any

from telegram import InlineKeyboardMarkup

from telegram_manager.ai.agent import Agent, scrub
from telegram_manager.ai.config import DEFAULT_REPLIES, MAX_REPLIES
from telegram_manager.ai.gemini import GeminiError
from telegram_manager.bot import keyboards
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

HISTORY_KEEP = 30
TEXT_KEEP = 400
STOP_WORDS = re.compile(
    r"\b(stop|unsubscribe|band\s+karo|mat\s+bhejo|message\s+mat|don'?t\s+message|leave\s+me\s+alone|block\s+kar)\b", re.IGNORECASE
)
ADMIN_TAG, END_TAG = "[[ADMIN]]", "[[END]]"

RULES = """Tum ek aam insaan ki tarah Telegram par PRIVATE chat kar rahe ho{me}. Tumhe yeh kaam mila hai:
KAAM: {goal}

Kaise likhna hai:
- Bilkul normal insaan / dost ki tarah, chhote message (1-2 line). Samne wala jis bhasha aur script mein likhe (Hinglish, Hindi, English) usi mein jawab do. Emoji kam.
- "Main aapki madad ke liye hoon", "kaise help kar sakta hoon", "assistant", "manager" jaisi customer-care wali baat mat karo: tum koi sahayak nahi, ek aam insaan ho jo baat kar raha hai.
- Samne wale ki baat samajh na aaye ya adhoori lage (galat spelling, ulta-seedha sawaal), to andaza mat lagao: chhota sa "kya matlab?" ya "samjha nahi, dobara likho" poochho. Kabhi bhi ulta-seedha jawab mat banao.
- Apni taraf se tathya, daam, vaade ya jaankari mat banao. Jo KAAM mein nahi hai aur tumhe nahi pata, "pata nahi" bolo.
- OTP, password, card ya paisa kabhi mat maango aur kisi se aisi cheez mat bhejwao.
- Apne maalik / owner / admin ka naam, ya yeh ki kisne tumhe yeh kaam diya, kabhi mat batao.
- Samne wala seedha aur sachche man se poochhe ki tum AI / bot ho, to jhooth mat bolo: bolo ki haan, tum AI ho (bas itna, kisi ka naam ya detail nahi).
- Samne wala gaali de, dhamki de, ya aisi baat ho jo tum sambhal nahi sakte: sirf "[[ADMIN]]" likho aur uske baad ek line mein wajah. Tab koi message nahi jayega, {address} se poochha jayega.
- Kaam poora ho jaye to aakhri message ke baad "[[END]]" likho."""


class Conversations:
    def __init__(self, deps) -> None:
        self.deps = deps
        self.agent = Agent(deps)
        self.sleep = asyncio.sleep  # tests make this instant
        self._cache: dict[int, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._drafts: dict[str, tuple[int, str, str]] = {}  # token -> (owner, chat id, text)

    # --- storage -------------------------------------------------------------------------------
    async def sessions(self, owner_id: int) -> list[dict[str, Any]]:
        if owner_id not in self._cache:
            self._cache[owner_id] = await self.deps.store.settings.get_ai_chats(owner_id)
        return self._cache[owner_id]

    async def _save(self, owner_id: int) -> None:
        live = [s for s in self._cache.get(owner_id, []) if s["state"] != "ended"][-50:]
        self._cache[owner_id] = live
        await self.deps.store.settings.set_ai_chats(owner_id, live)

    async def find(self, owner_id: int, sid: str) -> dict[str, Any] | None:
        return next((s for s in await self.sessions(owner_id) if s["id"] == sid), None)

    async def start(
        self, owner_id: int, account_id: str, peer_id: int, label: str, goal: str, autonomy: str,
        max_replies: int, opener: str = "",
    ) -> dict[str, Any]:
        sessions = await self.sessions(owner_id)
        for old in sessions:
            if old["account_id"] == account_id and old["peer_id"] == peer_id:
                old["state"] = "ended"  # one chat per person and account
        session = {
            "id": secrets.token_hex(3), "account_id": account_id, "peer_id": int(peer_id), "label": label,
            "goal": goal[:1500], "autonomy": "auto" if autonomy == "auto" else "draft",
            "replies_left": max(1, min(int(max_replies or DEFAULT_REPLIES), MAX_REPLIES)),
            "state": "active", "history": [], "started": int(time.time()),
        }
        if opener:
            session["history"].append({"who": "me", "text": opener[:TEXT_KEEP]})
        sessions.append(session)
        await self._save(owner_id)
        return session

    async def end(self, owner_id: int, sid: str | None = None, label: str | None = None) -> list[dict[str, Any]]:
        """End one chat (by id or label), or every chat when neither is given."""
        wanted = (label or "").strip().lstrip("@").lower()
        ended = []
        for s in await self.sessions(owner_id):
            if s["state"] == "ended":
                continue
            if sid and s["id"] != sid:
                continue
            if wanted and wanted not in s["label"].lower().lstrip("@"):
                continue
            s["state"] = "ended"
            ended.append(s)
        if ended:
            await self._save(owner_id)
        return ended

    async def resume(self, owner_id: int, label: str) -> list[dict[str, Any]]:
        wanted = (label or "").strip().lstrip("@").lower()
        resumed = [s for s in await self.sessions(owner_id) if s["state"] == "paused" and wanted in s["label"].lower().lstrip("@")]
        for s in resumed:
            s["state"] = "active"
        if resumed:
            await self._save(owner_id)
        return resumed

    # --- an incoming private message ---------------------------------------------------------
    async def on_incoming(self, account: dict, event: Any) -> None:
        if not getattr(event, "is_private", False) or getattr(event, "out", False):
            return  # groups, channels and our own messages are never answered
        owner_id = self.deps.manager._owner_of(account)
        account_id, peer_id = str(account["id"]), getattr(event, "sender_id", None)
        session = next(
            (s for s in await self.sessions(owner_id)
             if s["state"] in ("active", "paused") and s["account_id"] == account_id and s["peer_id"] == peer_id),
            None,
        )
        if session is None:
            return
        text = (getattr(event, "raw_text", "") or "").strip() or "[media]"
        session["history"].append({"who": "them", "text": text[:TEXT_KEEP]})
        session["history"] = session["history"][-HISTORY_KEEP:]
        if session["state"] == "paused":
            await self._save(owner_id)
            return
        if STOP_WORDS.search(text):
            session["state"] = "ended"
            await self._save(owner_id)
            await self._tell(owner_id, f"⛔ {session['label']} ne rokne ko kaha, isliye AI chat band kar di.\n💬 “{text[:200]}”")
            return
        self.deps.manager._spawn(self._turn(owner_id, session["id"]))

    async def _turn(self, owner_id: int, sid: str) -> None:
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session = await self.find(owner_id, sid)
            if session is None or session["state"] != "active" or not session["history"] or session["history"][-1]["who"] != "them":
                return  # already answered, or no longer running
            await self._mark_seen(owner_id, session)
            try:
                await self._reply(owner_id, session)
            except Exception as exc:
                logger.warning("AI chat turn failed: %s", describe_error(exc))
                await self._tell(owner_id, f"⚠️ {session['label']} ke saath AI chat mein dikkat: {describe_error(exc)}")

    async def _mark_seen(self, owner_id: int, session: dict[str, Any]) -> None:
        """Open the chat and mark the person's message READ (blue ticks) before the answer is written, like a person does."""
        try:
            manager = self.deps.manager
            client = manager._client_for(session["account_id"], owner_id)
            entity = await manager._known_entity(session["account_id"], client, session["peer_id"])
            await client.send_read_acknowledge(entity)
        except Exception:
            logger.debug("AI chat: the chat could not be marked read", exc_info=True)

    def _me(self, session: dict[str, Any]) -> str:
        account = self.deps.manager.accounts.get(session["account_id"]) or {}
        name = str(account.get("display_name") or "").strip()
        return f" (tumhara naam: {name})" if name else ""

    async def _reply(self, owner_id: int, session: dict[str, Any]) -> None:
        settings, keys = await self.agent.load(owner_id)
        if not keys:
            session["state"] = "paused"
            await self._save(owner_id)
            await self._tell(owner_id, f"⚠️ {settings.provider_label} key nahi hai, isliye AI chat ruki hui hai. /aisettings → 🔑 Keys.")
            return
        # A private chat is NOT the panel manager: it gets its own person-like prompt, never the manager prompt.
        system = RULES.format(address=settings.address, goal=session["goal"], me=self._me(session))
        try:
            reply = await self.deps.ai_client(settings).generate(keys, settings.active_model, system, _contents(session["history"]), temperature=0.6)
        except GeminiError as exc:
            await self._tell(owner_id, f"⚠️ AI chat ({session['label']}): {exc}")
            return
        text = "".join(p.get("text", "") for p in reply["parts"] if not p.get("thought")).strip()
        if not text or text.startswith(ADMIN_TAG):
            session["state"] = "paused"
            await self._save(owner_id)
            reason = text.removeprefix(ADMIN_TAG).strip() or "AI jawab nahi de paya"
            last = session["history"][-1]["text"]
            await self._tell(
                owner_id,
                f"✋ {session['label']} ki chat ruk gayi, aapka faisla chahiye.\n\n👤 Unhone likha: “{last[:300]}”\n🤖 Wajah: {reason}\n\n"
                "Bolo kya bhejna hai (\"usko ye bhejo: …\"), ya chat band/jaari karne ko kaho.",
                self._stop_markup(session),
            )
            return
        finish = END_TAG in text
        text = scrub(text.replace(END_TAG, "").strip())[:4000]
        if not text:
            return
        if session["autonomy"] == "draft":
            token = secrets.token_hex(4)
            self._drafts[token] = (owner_id, session["id"], text)
            while len(self._drafts) > 50:
                self._drafts.pop(next(iter(self._drafts)))
            await self._tell(
                owner_id,
                f"✍️ Draft — {session['label']} ke liye\n👤 {session['history'][-1]['text'][:300]}\n\n🤖 {text}",
                self._draft_markup(token, session["id"]),
            )
            return
        await self._send(owner_id, session, text, finish)

    async def send_draft(self, owner_id: int, token: str) -> str:
        entry = self._drafts.pop(token, None)
        if entry is None or entry[0] != owner_id:
            return "⌛ Yeh draft purana ho gaya."
        session = await self.find(owner_id, entry[1])
        if session is None or session["state"] == "ended":
            return "Yeh chat ab band hai."
        await self._send(owner_id, session, entry[2], False, announce=False)
        return f"✅ {session['label']} ko bhej diya."

    def skip_draft(self, token: str) -> None:
        self._drafts.pop(token, None)

    async def _send(self, owner_id: int, session: dict[str, Any], text: str, finish: bool, announce: bool = True) -> None:
        manager = self.deps.manager
        account_id = session["account_id"]
        client = manager._client_for(account_id, owner_id)
        entity = await manager._known_entity(account_id, client, session["peer_id"])
        delay = min(8.0, 1.5 + len(text) / 25) * random.uniform(0.8, 1.3)  # a typing pause, not an instant reply
        try:
            async with client.action(entity, "typing"):
                await self.sleep(delay)
        except Exception:
            await self.sleep(delay)
        sent = await client.send_message(entity, text, parse_mode=None)
        manager._remember_sent(account_id, str(session["peer_id"]), int(getattr(sent, "id", 0) or 0), owner_id)
        session["history"].append({"who": "me", "text": text[:TEXT_KEEP]})
        session["replies_left"] -= 1
        ending = finish or session["replies_left"] <= 0
        if ending:
            session["state"] = "ended"
        await self._save(owner_id)
        if announce:
            note = "\n🏁 Chat poori hui." if finish else ("\n🏁 Reply limit poora, chat band." if ending else f"\n({session['replies_left']} reply baaki)")
            their = next((h["text"] for h in reversed(session["history"][:-1]) if h["who"] == "them"), "")
            await self._tell(
                owner_id, f"🤖 {session['label']}\n👤 {their[:200]}\n💬 {text[:300]}{note}", None if ending else self._stop_markup(session)
            )

    # --- messages to the admin -----------------------------------------------------------------
    @staticmethod
    def _stop_markup(session: dict[str, Any]) -> InlineKeyboardMarkup:
        return keyboards._markup([[keyboards.Btn("⛔ Chat band karo", callback_data=f"aic|stop|{session['id']}")]])

    @staticmethod
    def _draft_markup(token: str, sid: str) -> InlineKeyboardMarkup:
        btn = keyboards.Btn
        return keyboards._markup([[btn("✅ Bhejo", callback_data=f"aic|send|{token}", style="success"), btn("⏭ Skip", callback_data=f"aic|skip|{token}")],
                                  [btn("⛔ Chat band karo", callback_data=f"aic|stop|{sid}", style="danger")]])

    async def _tell(self, owner_id: int, text: str, markup: Any = None) -> None:
        bot = self.deps.manager.bot
        if bot is None:
            return
        with suppress(Exception):
            await bot.send_message(chat_id=owner_id, text=text[:4000], reply_markup=markup)


def _contents(history: list[dict[str, str]]) -> list[dict]:
    """The talk as Gemini wants it: user/model turns that alternate, starting with the other person."""
    contents: list[dict] = []
    for item in history:
        role = "user" if item["who"] == "them" else "model"
        text = scrub(item["text"])
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": role, "parts": [{"text": text}]})
    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(chat abhi shuru hui; pehla message tumne bheja hai)"}]})
    return contents
