"""The AI: /ai, plain messages, approval buttons and the settings screen (/aisettings)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress

from telegram_manager.ai import tools
from telegram_manager.ai.agent import Agent
from telegram_manager.ai.config import (
    DEFAULT_PROMPT,
    MAX_KEYS,
    MODE_AUTO,
    MODE_CONFIRM,
    MODEL_NAME_RE,
    NAME_MAX,
    PROMPT_MAX,
    PROVIDER_GOOGLE,
    PROVIDER_LABELS,
    PROVIDER_OPENROUTER,
    PROVIDERS,
    UI_AI,
    UI_MANUAL,
    AISettings,
    mask_key,
)
from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase

logger = logging.getLogger(__name__)

WAIT_KEY = "ai_wait"
WAIT_SECONDS = 600
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")  # a Google (Gemini) key
OR_KEY_RE = re.compile(r"sk-or-[0-9A-Za-z_\-]{20,}")  # an OpenRouter key
HEX_RE = re.compile(r"\b[0-9a-fA-F]{32}\b")
PREFIX_RE = re.compile(r"^\s*ai\s*[,:\-]?\s+", re.IGNORECASE)
TYPING_TICK_SECONDS = 3  # the typing sign lasts about 5 s, so it is renewed this often
SLOW_AFTER_SECONDS = 6  # a quicker answer shows only the typing sign; a slower one also gets the "kaam chal raha hai" message
MAX_WAITING = 3  # messages that may wait in line behind the running one (per admin)
FIND_KEY = "ai_find"  # the part of the prompt waiting for its replacement
Btn, _markup = keyboards.Btn, keyboards._markup


def _row(*pairs: tuple[str, str]) -> list:
    return [Btn(label, callback_data=data) for label, data in pairs]


class AIHandlers(HandlerBase):
    def __init__(self, deps, engine) -> None:
        super().__init__(deps, engine)
        self.agent = Agent(deps)
        self._busy: set[int] = set()  # admins whose AI request is running right now
        self._locks: dict[int, asyncio.Lock] = {}  # one request at a time per admin; the next ones wait their turn (FIFO)
        self._in_line: dict[int, int] = {}  # running + waiting requests per admin

    # --- settings access -----------------------------------------------------------------
    async def _settings(self, uid: int) -> AISettings:
        return AISettings.from_dict(await self.store.settings.get_ai(uid))

    async def _keys(self, uid: int) -> list[str]:
        return (await self.agent.load(uid))[1]

    async def _save_keys(self, uid: int, keys: list[str], provider: str | None = None) -> None:
        """Save the keys of one provider (the one in use unless told otherwise)."""
        box = self.manager.secret_box
        field = "or_keys_enc" if (provider or (await self._settings(uid)).provider) == PROVIDER_OPENROUTER else "keys_enc"
        await self.store.settings.set_ai(uid, **{field: [box.encrypt(k) for k in keys]})

    def _client_of(self, provider: str):
        return self.deps.openrouter if provider == PROVIDER_OPENROUTER else self.deps.gemini

    def _decrypt(self, tokens) -> list[str]:
        keys = []
        for token in tokens:
            try:
                keys.append(self.manager.secret_box.decrypt(token))
            except Exception:
                logger.warning("A saved AI key could not be decrypted", exc_info=True)
        return keys

    # --- entry points used by the text handler ------------------------------------------
    @staticmethod
    def has_prefix(text: str) -> bool:
        return PREFIX_RE.match(text) is not None

    async def pre_route(self, update, context, text: str) -> bool:
        """Settings answers and secrets typed into the chat. True when this message was one of those."""
        uid = self.uid(update)
        wait = context.user_data.get(WAIT_KEY)
        if wait and time.time() - wait[1] < WAIT_SECONDS:
            context.user_data.pop(WAIT_KEY, None)
            await self._apply_wait(update, context, wait[0], text)
            return True
        context.user_data.pop(WAIT_KEY, None)
        message = update.effective_message
        found = {PROVIDER_GOOGLE: KEY_RE.findall(text), PROVIDER_OPENROUTER: OR_KEY_RE.findall(text)}
        if any(found.values()):
            lines = []
            for provider, keys in found.items():
                if keys:
                    added, skipped = await self._add_keys(uid, keys, provider)
                    lines.append(f"{PROVIDER_LABELS[provider]}: {added} nayi key save hui{self._skipped_note(skipped)}")
            with suppress(Exception):
                await message.delete()
            await message.reply_text(
                "🔑 " + " · ".join(lines) + ". Message delete kar diya taaki key chat mein na rahe.",
                reply_markup=_markup([_row(("🔑 Keys dekho", "ai|keys"))]),
            )
            return True
        creds = self._api_creds(text)
        if creds:
            await self.manager.save_api(uid, *creds)
            with suppress(Exception):
                await message.delete()
            await message.reply_text(f"✅ API ID {creds[0]} and its hash are saved (encrypted). Message deleted.")
            return True
        return False

    @staticmethod
    def _api_creds(text: str) -> tuple[int, str] | None:
        if len(text) > 300 or "api" not in text.lower():
            return None
        digest, ids = HEX_RE.search(text), re.findall(r"\b\d{5,10}\b", text)
        return (int(ids[0]), digest.group(0)) if digest and ids else None

    async def ui_mode(self, uid: int) -> str:
        """'ai', 'manual' or '' (not chosen yet) as picked on /start."""
        return (await self._settings(uid)).ui_mode

    async def not_answered(self, update, mode: str) -> None:
        """Plain text the AI did not take: Manual mode, AI switched off, or no key yet."""
        message, uid = update.effective_message, self.uid(update)
        if mode == UI_MANUAL:
            await message.reply_text(
                "🖐 Manual mode chalu hai — buttons use karo (/menu). AI se kaam karwana ho to neeche dabao 👇",
                reply_markup=_markup([_row(("🤖 AI Mode", "mode|ai"))]),
            )
        elif mode == UI_AI:
            current = await self._settings(uid)
            if not current.enabled:
                why, button = "AI OFF hai", ("✅ AI ON karo", "ai|toggle")
            else:
                why, button = f"{current.provider_label} API key nahi hai", ("🔑 Key jodo", "ai|keys")
            await message.reply_text(f"🤖 AI mode chalu hai, par {why}. Pehle yeh theek karo 👇", reply_markup=_markup([_row(button)]))
        else:
            await message.reply_text(
                "Use /start or /menu to open the account manager.\n🤖 AI se kaam karwane ke liye /aisettings mein OpenRouter (ya Gemini) key jodo."
            )

    async def ask(self, update, context, text: str, replied=None) -> bool:
        """Give a message to the AI. False when the AI is switched off.

        One request runs at a time per admin and the next ones wait in line (so a tap on a question button is
        never lost and the memory of the talk is never changed by two turns at once)."""
        uid = self.uid(update)
        message = update.effective_message
        loaded = await self.agent.load(uid)  # settings + keys, read once for the whole turn
        if not loaded[0].enabled or not loaded[1]:
            return False  # nothing to ask: the caller shows the normal hint
        if self._in_line.get(uid, 0) > MAX_WAITING:
            with suppress(Exception):
                await message.reply_text("⏳ Bahut saare kaam line mein hain — pehle wale poore hone do, phir naya bolo.")
            return True
        text = PREFIX_RE.sub("", text, count=1) if self.has_prefix(text) else text
        replied_text = None
        if replied is not None:
            replied_text = getattr(replied, "text", None) or getattr(replied, "caption", None)
        lock = self._locks.setdefault(uid, asyncio.Lock())
        waiting = lock.locked()
        self._in_line[uid] = self._in_line.get(uid, 0) + 1
        try:
            if waiting:
                with suppress(Exception):
                    await message.reply_text("🕐 Pichla kaam chal raha hai — iska number uske baad aayega.")
            async with lock:
                if waiting:  # settings may have changed while this one waited
                    loaded = await self.agent.load(uid)
                    if not loaded[0].enabled or not loaded[1]:
                        return False
                self._busy.add(uid)
                try:
                    result = await self._run_with_progress(update, context, uid, text, replied_text, loaded)
                finally:
                    self._busy.discard(uid)
                if result is None:
                    await message.reply_text("⚠️ AI mein kuch gadbad ho gayi. Dobara try karo.")
                    return True
                await self._present(update, context, result, loaded[0])
                return True
        finally:
            left = self._in_line.get(uid, 1) - 1
            if left > 0:
                self._in_line[uid] = left
            else:
                self._in_line.pop(uid, None)
                self._locks.pop(uid, None)

    async def _run_with_progress(self, update, context, uid: int, text: str, replied_text, loaded):
        """Run the agent. It starts at once; the typing sign shows from the first second, and only if the
        answer takes longer than SLOW_AFTER_SECONDS a "kaam chal raha hai" message is added (removed at the end)."""
        chat_id = update.effective_chat.id
        work = asyncio.ensure_future(self.agent.run(update, context, uid, text, replied_text, loaded))
        started, status = time.monotonic(), None
        try:
            await self._typing(context, chat_id)
            while True:
                done, _ = await asyncio.wait({work}, timeout=TYPING_TICK_SECONDS)
                if done:
                    break
                await self._typing(context, chat_id)
                elapsed = time.monotonic() - started
                if elapsed >= SLOW_AFTER_SECONDS:
                    status = await self._show_status(context, chat_id, status, int(elapsed))
            return work.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI turn failed")
            return None
        finally:
            if not work.done():
                work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work
            if status is not None:
                with suppress(Exception):
                    await context.bot.delete_message(chat_id, status.message_id)

    @staticmethod
    async def _typing(context, chat_id: int) -> None:
        with suppress(Exception):
            await context.bot.send_chat_action(chat_id, "typing")

    @staticmethod
    async def _show_status(context, chat_id: int, status, seconds: int):
        text = f"⏳ Kaam chal raha hai… {seconds} sec"
        with suppress(Exception):
            if status is None:
                return await context.bot.send_message(chat_id, text)
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status.message_id, text=text)
        return status

    async def _present(self, update, context, result, settings: AISettings) -> None:
        message = update.effective_message
        await self._show_blocks(update, result.outcome)  # a list of accounts, printed as it is, before the AI's comment
        if result.text:
            await message.reply_text(result.text[:4000])
        for token, summary in result.outcome.pending:
            await message.reply_text(
                f"🤖 {settings.name} ko aapki ijazat chahiye:\n\n{summary}",
                reply_markup=keyboards.confirm_pair(f"aia|ok|{token}", f"aia|no|{token}", "✅ Haan, karo"),
            )
        for question, token, options in result.outcome.questions:
            await message.reply_text(f"❓ {question}", reply_markup=_markup(self._option_rows(token, options)))
        if result.outcome.choices:
            rows = [[Btn(label, callback_data=data)] for label, data in result.outcome.choices[:20]]
            await message.reply_text(result.outcome.choices_title, reply_markup=_markup(rows))
        if result.outcome.wizard:
            answers: dict = {}
            saved = await self.manager.saved_api(self.uid(update))
            if saved:
                answers["api_id"], answers["api_hash"] = saved
            answers.update(result.outcome.wizard_answers)
            await self.engine.start(update, context, result.outcome.wizard, answers)

    @staticmethod
    async def _show_blocks(update, outcome) -> None:
        for block in outcome.display:
            await update.effective_message.reply_text(block[:4096])

    @staticmethod
    def _option_rows(token: str, options: list[str]) -> list[list]:
        """The buttons of a question the AI asked: short ones two to a row, long ones alone."""
        rows: list[list] = []
        for index, option in enumerate(options):
            button = Btn(option, callback_data=f"aiq|{token}|{index}")
            if len(option) <= 18 and rows and len(rows[-1]) == 1 and len(options[index - 1]) <= 18:
                rows[-1].append(button)
            else:
                rows.append([button])
        return rows

    async def question_button(self, update, context) -> None:
        """aiq|<token>|<n>: the admin tapped one of the AI's question buttons; the choice goes back to the AI."""
        if not await self.entry(update):
            return
        parts = (update.callback_query.data or "").split("|")
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        answer = tools.take_answer(context, parts[1] if len(parts) > 1 else "", index)
        if answer is None:
            await ui.show(update, "⌛ Yeh sawaal purana ho gaya. Dobara bolo.")
            return
        question, choice = answer
        await ui.show(update, f"❓ {question}\n➡️ {choice}")  # the buttons go away, the choice stays on screen
        if not await self.ask(update, context, f"[Maine tumhare sawaal «{question}» par yeh button dabaya: {choice}]"):
            await update.effective_message.reply_text("AI abhi chalu nahi hai: /aisettings mein ON karo aur API key jodo.")

    # --- commands ----------------------------------------------------------------------------
    async def keys_command(self, update, context) -> None:
        if await self.guard.allow(update):
            await self._show_keys(update, self.uid(update))

    async def new_chat(self, update, context) -> None:
        """🧹 Nayi Baat: the AI forgets the talk so far."""
        if await self.guard.allow(update):
            self.agent.clear(self.uid(update))
            await ui.show(update, "🧹 Purani baat bhool gaya. Nayi baat shuru — bolo!", keyboards.ai_panel_keyboard())

    async def ai_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        text = self.command_args(update).strip()
        if not text:
            await self.show_settings(update)
            return
        if await self.pre_route(update, context, text):
            return
        if not await self.ask(update, context, text, update.effective_message.reply_to_message):
            await ui.show(update, "AI abhi chalu nahi hai: /aisettings mein ON karo aur API key jodo.", keyboards.back("ai|home", "🤖 AI"))

    async def settings_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        await self.show_settings(update)

    async def clear_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        self.agent.clear(self.uid(update))
        await ui.show(update, "🧹 AI ki yaad saaf. Nayi baat se shuru.", keyboards.back("ai|home", "🤖 AI"))

    # --- the settings screen --------------------------------------------------------------------
    async def show_settings(self, update) -> None:
        uid = self.uid(update)
        s, keys = await self._settings(uid), await self._keys(uid)
        prompt = s.prompt if len(s.prompt) <= 350 else s.prompt[:350] + "…"
        text = (
            f"🤖 AI — {s.name}  [{'ON ✅' if s.enabled else 'OFF ⛔'}]\n\n"
            f"👤 Aapko bulata hai: {s.address}\n"
            f"🔌 Provider: {s.provider_label}\n"
            f"🧠 Model: {s.active_model}\n"
            f"🧩 Mode: {'✋ Confirm — bhejne/delete se pehle puchta hai' if s.mode == MODE_CONFIRM else '⚡ Auto — seedha kar deta hai'}\n"
            f"🔑 {s.provider_label} keys: {len(keys)}/{MAX_KEYS}\n\n"
            f"📝 Prompt:\n{prompt}\n\n"
            f"Kaise use karo: seedha likho (jaise \"account 3 se @user ko hello bhejo\"), ya /ai <baat>. "
            f"Kisi message par reply karke \"ai ...\" likhoge to {s.name} woh message padhkar samjhega."
        )
        rows = [
            _row((("⛔ OFF karo" if s.enabled else "✅ ON karo"), "ai|toggle"), ("🧩 Mode badlo", "ai|mode")),
            _row(("📝 Prompt (badlo / jodo)", "ai|prompts"), ("🔑 Keys", "ai|keys")),
            _row(("🏷 AI ka naam", "ai|name"), ("👤 Mujhe kya bulaye", "ai|address")),
            _row(("🔌 Provider", "ai|provider"), ("🧠 Model", "ai|model")),
            _row(("🧹 Yaad saaf", "ai|forget")),
            _row(("⬅️ AI Home", "ai|panel") if s.ui_mode == UI_AI else ("⬅️ Menu", "menu")),
        ]
        await ui.show(update, text, _markup(rows))

    async def show_panel(self, update) -> None:
        """The home screen of AI mode."""
        uid = self.uid(update)
        s, keys = await self._settings(uid), await self._keys(uid)
        state = "✅ taiyaar" if s.enabled and keys else ("⛔ OFF hai" if keys else f"⚠️ {s.provider_label} key chahiye")
        how = "✋ Confirm: bhejne/delete se pehle puchunga" if s.mode == MODE_CONFIRM else "⚡ Auto: seedha kar dunga"
        text = (
            f"🤖 AI Mode — {s.name}  [{state}]\n\n"
            "Bas normal message likho, baaki main kar dunga. Jaise:\n"
            "• “accounts ki list dikhao” · “naam se sajao”\n"
            "• “ban / limited account dhundo” · “sab check karo”\n"
            "• “Rahul se @user ko hello bhejo”\n"
            "• “roz 6:30 baje broadcast lagao”\n"
            "• “prompt mein yeh rule jodo: …”\n\n"
            f"🧩 {how}\n🔌 {s.provider_label} · 🧠 {s.active_model} · 🔑 {len(keys)} key\n"
            "Kuch saaf na hua to main buttons se puchunga."
        )
        if not keys:
            text += "\n\n🔑 Pehle API key jodo: neeche 🔑 Keys dabao, ya key seedha yahin paste kar do."
        await ui.show(update, text, keyboards.ai_panel_keyboard())

    async def show_prompts(self, update, s: AISettings | None = None) -> None:
        """Everything about the prompt: replace, add to it, change one part, read it, undo, reset."""
        s = s or await self._settings(self.uid(update))
        preview = s.prompt if len(s.prompt) <= 700 else s.prompt[:700] + "…"
        text = (
            f"📝 {s.name} ka prompt — {len(s.prompt)}/{PROMPT_MAX} akshar\n\n{preview}\n\n"
            "✏️ Poora badlo · ➕ Aage jodo · 🔎 Ek hissa badlo.\n"
            "💬 Ya chat mein hi bolo: “prompt mein yeh rule jodo: …” — haan dabane par hi badlega."
        )
        rows = [
            _row(("✏️ Poora badlo", "ai|prompt"), ("➕ Aage jodo", "ai|prompt_add")),
            _row(("🔎 Ek hissa badlo", "ai|prompt_find"), ("👁 Poora dekho", "ai|showprompt")),
            _row(("↩️ Pichhla wapas" if s.prompt_prev else "↩️ (koi purana nahi)", "ai|promptundo"), ("♻️ Default", "ai|promptreset")),
            _row(("⬅️ Back", "ai|home")),
        ]
        await ui.show(update, text, _markup(rows))

    async def _set_prompt(self, uid: int, new: str) -> None:
        """Save a new prompt and keep the one it replaces, so ↩️ can bring it back."""
        old = (await self._settings(uid)).prompt
        await self.store.settings.set_ai(uid, prompt=new, prompt_prev=old)

    async def settings_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, arg = (parts[1] if len(parts) > 1 else "home"), (parts[2] if len(parts) > 2 else "")
        s = await self._settings(uid)
        if action == "home":
            await self.show_settings(update)
        elif action == "toggle":
            await self.store.settings.set_ai(uid, enabled=not s.enabled)
            await self.show_settings(update)
        elif action == "mode":
            await self.store.settings.set_ai(uid, mode=MODE_AUTO if s.mode == MODE_CONFIRM else MODE_CONFIRM)
            await self.show_settings(update)
        elif action == "panel":
            await self.show_panel(update)
        elif action == "newchat":
            self.agent.clear(uid)
            await self.show_panel(update)
        elif action == "chats":
            await self.chats_command(update, context)
        elif action == "prompts":
            await self.show_prompts(update, s)
        elif action in ("prompt", "prompt_add", "prompt_find", "name", "address", "model_custom", "key_add"):
            await self._ask_for(update, context, action)
        elif action == "showprompt":
            await self._send_prompt(update, s)
        elif action == "promptreset":
            await self._set_prompt(uid, DEFAULT_PROMPT)
            await self.show_prompts(update)
        elif action == "promptundo":
            if s.prompt_prev:
                await self._set_prompt(uid, s.prompt_prev)  # swaps: undo twice = redo
            await self.show_prompts(update)
        elif action == "forget":
            self.agent.clear(uid)
            await self.show_settings(update)
        elif action == "provider":
            await self._show_provider(update, s)
        elif action == "provider_set":
            if arg.isdigit() and int(arg) < len(PROVIDERS):
                await self.store.settings.set_ai(uid, provider=PROVIDERS[int(arg)])
            await self.show_settings(update)
        elif action == "model":
            await self._show_models(update, s)
        elif action == "model_set":
            if arg.isdigit() and int(arg) < len(s.model_presets):
                await self.store.settings.set_ai(uid, **{s.model_field: s.model_presets[int(arg)]})
            await self.show_settings(update)
        elif action == "keys":
            await self._show_keys(update, uid)
        elif action == "key_del":
            keys = await self._keys(uid)
            if arg.isdigit() and int(arg) < len(keys):
                self._forget(s.provider, keys.pop(int(arg)))
                await self._save_keys(uid, keys)
            await self._show_keys(update, uid)
        elif action == "key_test":
            await self._test_keys(update, uid, s)
        elif action == "key_reset":
            for key in await self._keys(uid):
                self._forget(s.provider, key)  # resting / rejected keys get another chance
            await self._show_keys(update, uid)
        elif action == "key_clear":
            count = len(await self._keys(uid))
            await ui.show(
                update, f"🗑 Saari {count} {s.provider_label} key hatani hain? AI tab tak band rahegi jab tak nayi key na jodo.",
                keyboards.confirm_pair("ai|key_clear_ok", "ai|keys", "✅ Haan, sab hatao"),
            )
        elif action == "key_clear_ok":
            for key in await self._keys(uid):
                self._forget(s.provider, key)
            await self._save_keys(uid, [])
            await self._show_keys(update, uid)

    async def _ask_for(self, update, context, field: str) -> None:
        field = "model" if field == "model_custom" else ("keys" if field == "key_add" else field)
        s = await self._settings(self.uid(update))
        context.user_data[WAIT_KEY] = (field, time.time())
        prompts = {
            "prompt": f"✏️ Naya prompt bhejo (max {PROMPT_MAX} akshar). {{name}} aur {{address}} likhoge to unki jagah AI ka naam aur aapka naam aa jayega.",
            "prompt_add": "➕ Jo rule / line prompt ke end mein jodni hai woh bhejo. Purana prompt waisa hi rahega.",
            "prompt_find": "🔎 Prompt ka woh hissa bhejo jo badalna hai (bilkul waisa hi jaisa prompt mein likha hai). Poora prompt dekhna ho to pehle 👁 dabao.",
            "name": f"🏷 AI ka naya naam bhejo (max {NAME_MAX} akshar).",
            "address": f"👤 AI aapko kya bulaye? Jaise: Mandal Sir (max {NAME_MAX} akshar).",
            "model": ("🧠 OpenRouter model ka naam bhejo — koi bhi chalega, jaise openrouter/free ya qwen/qwen3-next-80b-a3b-instruct:free. "
                      "Tool-calling wala model chuno, warna AI kaam nahi kar payega.") if s.is_openrouter else "🧠 Gemini model ka naam bhejo, jaise gemini-flash-latest",
            "keys": f"🔑 {s.provider_label} API key bhejo ({'sk-or-… se shuru' if s.is_openrouter else 'AIza… se shuru'}). Ek se zyada ho to alag lines mein ek saath bhej do. Message turant delete ho jayega.",
        }
        await ui.show(update, prompts[field] + "\n\n(10 minute tak intezaar karunga.)", keyboards.back("ai|home", "❌ Cancel"))

    async def _apply_wait(self, update, context, field: str, text: str) -> None:
        uid, message = self.uid(update), update.effective_message
        text = text.strip()
        if field == "keys":
            tokens = [t for t in re.split(r"[\s,;]+", text) if len(t) >= 20 and t.isascii()]
            with suppress(Exception):
                await message.delete()
            if not tokens:
                await message.reply_text("❌ Koi key nahi mili. /aisettings → 🔑 Keys se dobara try karo.")
                return
            groups: dict[str, list[str]] = {}
            for token in tokens:  # AIza… is Google's, sk-or-… is OpenRouter's; anything else goes to the provider in use
                provider = PROVIDER_GOOGLE if token.startswith("AIza") else PROVIDER_OPENROUTER if token.startswith("sk-or-") else None
                groups.setdefault(provider or (await self._settings(uid)).provider, []).append(token)
            notes = []
            for provider, group in groups.items():
                added, skipped = await self._add_keys(uid, group, provider)
                notes.append(f"{PROVIDER_LABELS[provider]}: {added} nayi key save hui{self._skipped_note(skipped)}")
            await message.reply_text("🔑 " + " · ".join(notes) + " (message delete kar diya).")
            await self._show_keys(update, uid)
            return
        error = None
        if field == "prompt":
            if len(text) < 20 or len(text) > PROMPT_MAX:
                error = f"Prompt 20 se {PROMPT_MAX} akshar ke beech hona chahiye."
            else:
                await self._set_prompt(uid, text)
        elif field == "prompt_add":
            current = (await self._settings(uid)).prompt
            new = current.rstrip() + "\n" + text
            if len(text) < 3:
                error = "Kuch likho jo jodna hai (kam se kam 3 akshar)."
            elif len(new) > PROMPT_MAX:
                error = f"Itna jodne par prompt {len(new)} akshar ka ho jayega; limit {PROMPT_MAX} hai."
            else:
                await self._set_prompt(uid, new)
        elif field == "prompt_find":
            count = (await self._settings(uid)).prompt.count(text) if text else 0
            if not count:
                error = "Yeh hissa prompt mein nahi mila. 👁 Poora dekho se copy karke bhejo."
            else:
                context.user_data[FIND_KEY] = text
                context.user_data[WAIT_KEY] = ("prompt_replace", time.time())
                await message.reply_text(
                    f"✅ Mil gaya ({count} jagah). Ab uski jagah kya likhna hai? Hatana ho to sirf - bhejo.",
                    reply_markup=keyboards.back("ai|prompts", "❌ Cancel"),
                )
                return
        elif field == "prompt_replace":
            old = context.user_data.pop(FIND_KEY, "")
            current = (await self._settings(uid)).prompt
            new = current.replace(old, "" if text == "-" else text) if old and old in current else ""
            if not old or not new:
                error = "Woh hissa ab prompt mein nahi hai. Dobara 🔎 se shuru karo."
            elif not 20 <= len(new) <= PROMPT_MAX:
                error = f"Badalne ke baad prompt {len(new)} akshar ka hoga; 20 se {PROMPT_MAX} ke beech chahiye."
            else:
                await self._set_prompt(uid, new)
        elif field in ("name", "address"):
            if not 1 <= len(text) <= NAME_MAX or "\n" in text:
                error = f"1 se {NAME_MAX} akshar, ek line mein."
            else:
                await self.store.settings.set_ai(uid, **{field: text})
        elif field == "model":
            model = text.removeprefix("models/")
            if not re.fullmatch(MODEL_NAME_RE, model):
                error = "Model ka naam sirf akshar, number aur . - _ / : ho sakta hai (jaise openrouter/free)."
            else:
                await self.store.settings.set_ai(uid, **{(await self._settings(uid)).model_field: model})
        if error:
            await message.reply_text(f"❌ {error}")
            return
        await message.reply_text("✅ Save ho gaya.")
        if field.startswith("prompt"):
            await self.show_prompts(update)
        else:
            await self.show_settings(update)

    def _forget(self, provider: str, key: str) -> None:
        forget = getattr(self._client_of(provider), "forget", None)
        if forget:
            forget(key)

    async def _add_keys(self, uid: int, new: list[str], provider: str | None = None) -> tuple[int, int]:
        """Save new keys of one provider (up to MAX_KEYS). Returns (added, skipped): skipped = already saved or over the limit."""
        s = await self._settings(uid)
        provider = provider or s.provider
        keys = self._decrypt(s.or_keys_enc if provider == PROVIDER_OPENROUTER else s.keys_enc)
        added = 0
        unique = list(dict.fromkeys(new))
        for key in unique:
            if key not in keys and len(keys) < MAX_KEYS:
                keys.append(key)
                self._forget(provider, key)
                added += 1
        if added:
            await self._save_keys(uid, keys, provider)
            if provider != s.provider and not s.active_keys_enc:
                await self.store.settings.set_ai(uid, provider=provider)  # the provider in use had no key: use the one just given
        return added, len(unique) - added

    @staticmethod
    def _skipped_note(skipped: int) -> str:
        return f" ({skipped} pehle se thi ya limit {MAX_KEYS} poori)" if skipped else ""

    async def _send_prompt(self, update, s: AISettings) -> None:
        chat_id = update.effective_chat.id
        text = s.system_prompt()
        for start in range(0, len(text), 3800):
            await update.get_bot().send_message(chat_id, text[start:start + 3800])

    async def _show_provider(self, update, s: AISettings) -> None:
        rows = [[Btn(("✅ " if p == s.provider else "") + PROVIDER_LABELS[p], callback_data=f"ai|provider_set|{i}")] for i, p in enumerate(PROVIDERS)]
        rows.append(_row(("⬅️ Back", "ai|home")))
        await ui.show(
            update,
            f"🔌 Abhi: {s.provider_label}\n\n"
            "• OpenRouter — ek key se bahut saare models (free bhi). Shuruaat openrouter/free se hoti hai: woh khud ek free model chunta hai.\n"
            "• Google Gemini — seedha Google ki key.\n\n"
            "Har provider ki keys aur model alag save rehte hain: badalne par kuch delete nahi hota. "
            "Jis provider ki key nahi hai woh chalega nahi.",
            _markup(rows),
        )

    async def _show_models(self, update, s: AISettings) -> None:
        rows = [[Btn(("✅ " if m == s.active_model else "") + m, callback_data=f"ai|model_set|{i}")] for i, m in enumerate(s.model_presets)]
        rows.append(_row(("✏️ Dusra model likho", "ai|model_custom")))
        rows.append(_row(("⬅️ Back", "ai|home")))
        if s.is_openrouter:
            info = (
                f"🧠 {s.provider_label} model abhi: {s.active_model}\n\n"
                "• openrouter/free — OpenRouter khud ek free model chunta hai jo tools chala sake (default)\n"
                "• :free wale models muft hain par kabhi-kabhi limit / busy aate hain; zyada keys jodne se madad milti hai\n"
                "• ✏️ se koi bhi OpenRouter model likh sakte ho (paid bhi), bas tool-calling wala ho."
            )
        else:
            info = (
                f"🧠 {s.provider_label} model abhi: {s.active_model}\n\n• gemini-flash-latest — sabse samajhdar, asli kaam ke liye\n"
                "• flash-lite — sasta aur tez, chhote kaam ke liye\n\n-latest naam Google khud naye version par le jata hai."
            )
        await ui.show(update, info, _markup(rows))

    async def _show_keys(self, update, uid: int) -> None:
        s = await self._settings(uid)
        keys = await self._keys(uid)
        status_of = getattr(self._client_of(s.provider), "status", None)
        badge = {"ok": "✅", "resting": "💤", "rejected": "⛔"}
        lines, buttons = [], []
        for i, key in enumerate(keys):
            state = status_of(key) if status_of else "ok"
            lines.append(f"{i + 1}. {mask_key(key)} {badge.get(state, '✅')} "
                         + {"ok": "chal rahi", "resting": "limit par, 1 minute aaram", "rejected": ("OpenRouter" if s.is_openrouter else "Google") + " ne reject ki"}.get(state, ""))
            buttons.append(Btn(f"🗑 {i + 1} {mask_key(key)} {badge.get(state, '')}", callback_data=f"ai|key_del|{i}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append(_row(("➕ Key jodo", "ai|key_add"), ("🧪 Test karo", "ai|key_test")))
        if keys:
            rows.append(_row(("♻️ Aaram/reject wali dobara try", "ai|key_reset")))
            rows.append(_row(("🗑 Saari keys hatao", "ai|key_clear")))
        rows.append(_row(("⬅️ Back", "ai|home")))
        listing = ("\n".join(lines) + "\n\n") if lines else "Abhi koi key nahi hai.\n\n"
        await ui.show(
            update,
            f"🔑 {s.provider_label} keys: {len(keys)}/{MAX_KEYS}\n\n{listing}"
            f"➕ dabao aur ek saath 10 (ya zyada) keys alag-alag line mein paste kar do. Message turant delete hota hai, keys encrypted save hoti hain. "
            "Keys seedha chat mein paste karoge to bhi pehchan lunga (OpenRouter: sk-or-…, Google: AIza…).\n\n"
            "Keys baari-baari se use hoti hain; jo limit par pahunche woh 1 minute aaram karti hai, rejected key side mein rakh di jati hai. "
            + ("Alag-alag OpenRouter account ki keys ki limit alag hoti hai." if s.is_openrouter else "Alag-alag Google project ki keys ka quota alag hota hai."),
            _markup(rows),
        )

    async def _test_keys(self, update, uid: int, s: AISettings) -> None:
        keys = await self._keys(uid)
        if not keys:
            await ui.show(update, "Koi key nahi hai.", keyboards.back("ai|keys", "🔑 Keys"))
            return
        await ui.show(update, f"⏳ {len(keys)} key test ho rahi hain…")
        lines = []
        for i, key in enumerate(keys, start=1):
            problem = await self._client_of(s.provider).check_key(key, s.active_model)
            lines.append(f"{i}. {mask_key(key)} — {'✅ chal rahi hai' if problem is None else '❌ ' + problem[:120]}")
        await ui.show(update, "\n".join(lines), keyboards.back("ai|keys", "🔑 Keys"))

    # --- approvals and broadcast choice -----------------------------------------------------------
    async def approval_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, token = parts[1], parts[2] if len(parts) > 2 else ""
        settings = await self._settings(uid)
        ctx = tools.ToolContext(self.deps, update, context, uid, settings.mode)
        if action == "no":
            context.user_data.get(tools.PENDING_KEY, {}).pop(token, None)
            await ui.show(update, "❌ Theek hai, nahi kiya.")
            return
        summary = tools.pending_summary(ctx, token)
        result = await tools.execute_pending(ctx, token)
        if result is None:
            await ui.show(update, "⌛ Yeh request purani ho gayi. Dobara bolo.")
            return
        if result.get("error"):
            await ui.show(update, f"❌ {result['error']}")
        elif result.get("message"):
            await ui.show(update, result["message"])
        elif not result.get("status") == "started":
            await ui.show(update, f"✅ Ho gaya: {summary or ''}")
        await self._show_blocks(update, ctx.outcome)

    async def broadcast_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        parts = (update.callback_query.data or "").split("|")
        slot = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        removed = await self.manager.stop_daily_job(self.uid(update), slot)
        await ui.show(update, f"🗑 Daily broadcast #{slot} delete ho gaya." if removed else "Yeh broadcast ab nahi hai.")

    # --- the AI's own chats (/aichats) -------------------------------------------------------------
    async def chats_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        uid = self.uid(update)
        live = [s for s in await self.deps.chats.sessions(uid) if s["state"] != "ended"]
        if not live:
            await ui.show(
                update,
                "💬 Abhi koi AI chat nahi chal rahi.\n\nShuru karne ke liye bolo: \"Rahul se @user ko hello bolke chat karo, usse X ke baare mein baat karo\".",
                keyboards.back("ai|home", "🤖 AI"),
            )
            return
        lines, rows = ["💬 AI chats:\n"], []
        for s in live:
            row = await self.store.accounts.get(s["account_id"], owner_id=uid) or {}
            state = "▶️ chal rahi" if s["state"] == "active" else "⏸ ruki (aapka faisla chahiye)"
            mode = "khud reply" if s["autonomy"] == "auto" else "draft"
            lines.append(f"• {s['label']} — account {row.get('number', '?')}, {mode}, {s['replies_left']} reply baaki, {state}")
            rows.append([Btn(f"⛔ {s['label']}", callback_data=f"aic|stop|{s['id']}")])
        rows.append(_row(("⬅️ Back", "ai|home")))
        await ui.show(update, "\n".join(lines), _markup(rows))

    async def chat_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, arg = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
        if action == "stop":
            ended = await self.deps.chats.end(uid, sid=arg)
            await ui.show(update, f"⛔ {ended[0]['label']} ke saath AI chat band." if ended else "Yeh chat pehle hi band hai.")
        elif action == "send":
            await ui.show(update, await self.deps.chats.send_draft(uid, arg))
        elif action == "skip":
            self.deps.chats.skip_draft(arg)
            await ui.show(update, "⏭ Draft chhod diya. Agla message aane par naya draft banega.")
