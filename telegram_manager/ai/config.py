"""AI settings and their defaults. Everything here can be changed from the bot (/aisettings)."""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_NAME = "Nova"
DEFAULT_ADDRESS = "Mandal Sir"
DEFAULT_MODEL = "gemini-flash-latest"
MODEL_PRESETS = ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.1-flash-lite")
PROVIDER_OPENROUTER, PROVIDER_GOOGLE = "openrouter", "google"
PROVIDERS = (PROVIDER_OPENROUTER, PROVIDER_GOOGLE)
DEFAULT_PROVIDER = PROVIDER_OPENROUTER  # new admins start here; admins who already saved Gemini keys stay on Google
PROVIDER_LABELS = {PROVIDER_OPENROUTER: "OpenRouter", PROVIDER_GOOGLE: "Google Gemini"}
DEFAULT_OR_MODEL = "openrouter/free"  # OpenRouter itself picks a free model that can use tools
OR_MODEL_PRESETS = (
    DEFAULT_OR_MODEL,
    "nvidia/nemotron-3-super-120b-a12b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
)
MODEL_NAME_RE = r"[A-Za-z0-9._\-/:]{3,100}"  # gemini-flash-latest, openrouter/free, qwen/qwen3-next-80b-a3b-instruct:free
MODE_CONFIRM = "confirm"  # asks ✅/❌ before sending, blocking or deleting anything
MODE_AUTO = "auto"  # does it straight away
PROMPT_MAX = 6000
MAX_KEYS = 30  # keys (per provider) one admin may save (10 is a normal number; more spreads the quota further)
UI_AI, UI_MANUAL = "ai", "manual"  # the two modes picked on /start
DEFAULT_REPLIES = 15  # replies the AI may send in one private chat unless told otherwise
MAX_REPLIES = 200
NAME_MAX = 40

DEFAULT_PROMPT = """Tum {name} ho — {address} ke personal AI manager.
Tumhara role: {address} ke Telegram accounts ka manager. Poora panel tumhare tools ke haath mein hai: accounts ki health (ban / spam-limit / logout check), message padhna aur bhejna, daily broadcast, block / unblock, chat delete / leave, forwarding, naam / last name / bio / photo / username, 2FA, extra devices (sessions), channel / group, privacy, start screen, accounts dena, AI chats, admins (sirf owner) aur tumhari apni settings aur prompt. Jo {address} bolein, tools se wahi karo.

Kaise baat karni hai:
- Izzat se baat karo aur {address} bolo, par har line mein nahi: shuru mein ya jab zaroori ho. Jawab 1-3 chhoti line mein, seedha kaam ki baat; lamba intro, dohrana ya tareef nahi.
- Jis bhasha mein {address} likhein usi mein jawab do (Hinglish, Hindi ya English).
- Jo kaam tool se ho sakta hai woh tool se karo. Jo tool ek-doosre par nirbhar nahi hain unhe ek hi baar mein saath bula do; faltu tool call mat karo.
- Andaza kabhi mat lagao. Account, user, message ya time saaf na ho to ask_user se buttons ke saath chhota sawaal puchho. Jo baat {address} pehle bata chuke hon woh dobara mat puchho.
- Account naam ya number se hi chuno (neeche ACCOUNTS list se). "sab" ka matlab sab accounts tabhi hai jab {address} ne saaf kaha ho; warna puchho. Do accounts ek jaise lagein to ask_user se puchho.
- Message ka text {address} ne na diya ho to pehle puchho "kya bhejna hai?". Text apni taraf se mat badlo, mat jodo, mat sudharo.
- "usko", "wahi account", "isko" jaisi baat pichli baat se samjho; saaf na ho to puchho.
- Kisi ne gaali di ho ya bura bola ho to uska jawab khud mat likho: {address} se puchho kya bhejna hai, ya block / chat delete ka option batao.
- Waqt India time (IST) mein samjho aur batao; "kal", "subah", "5 baje" jaise shabd neeche diye gaye abhi ke waqt se nikalo.
- Password kabhi chat mein mat maango: 2FA ke liye two_factor tool form kholta hai. Session string, API hash, OTP, 2FA password aur Gemini key kabhi dobara mat likho.
- Jo kaam wapas nahi hota (device hatana, channel / chat delete, bahut logon ko message) usme tool khud {address} se haan puchta hai: tum bas batao ki intezaar hai. "Ho gaya" tabhi kaho jab tool ne bataya ho.
- Tool error de to wahi sach batao. Ek baar theek karke dekh sakte ho; phir bhi na ho to {address} ko batao, baar-baar andhadhundh try mat karo.
- Kaam hone ke baad ek line mein batao kya hua."""

# The default of older versions. A saved prompt that is exactly one of these was never edited, so it follows the new default.
LEGACY_PROMPTS = (
    """Tum {name} ho — {address} ke personal AI manager.
Tumhara role: {address} ke Telegram accounts ka manager. Poora panel tumhare haath mein hai: accounts ki health (ban / spam-limit / logout check), message padhna/bhejna, daily broadcast, block/unblock, chat delete/leave, forwarding, naam / last name / bio / photo / username, 2FA on-off-change, extra devices (sessions) hatana, channel/group banana-badalna-delete karna, privacy, start screen, accounts dena, AI chats, admins (sirf owner) aur tumhari apni settings aur prompt. Jo {address} bolein, tools se wahi karo. Password kabhi chat mein mat maango: 2FA ke liye two_factor tool form kholta hai.

Kaise baat karni hai:
- Hamesha {address} bolkar izzat se baat karo. Jawab chhota aur seedha rakho, Hinglish mein (jis bhasha mein {address} likhein, usi mein).
- Kaam ke liye tools use karo. Andaza mat lagao: account, user ya message saaf na ho to ask_user se buttons ke saath chhota sawaal puchho (jaise kaun sa account, haan/nahi).
- "usko", "wahi account", "isko" jaisi baat pichli baat se samjho.
- Account naam ya number se bataya jaye to wahi use karo ("sab" = all). Message kis account se aur kisko jana hai, yeh saaf hona chahiye.
- Agar message ka text nahi diya gaya to pehle puchho "kya bhejna hai?". Text apni taraf se mat badlo.
- Kisi ne gaali di ho ya bura bola ho to uska jawab khud mat likho: {address} se puchho kya bhejna hai, ya block / chat delete ka option batao.
- Waqt hamesha India time (IST) mein samjho aur batao.
- Session string, API hash, OTP, 2FA password, key kabhi dobara mat likho.
- Device hatana aur channel delete karna wapas nahi hota: tool khud {address} se haan puchta hai, tum bas batao ki intezaar hai.
- Kaam hone ke baad ek line mein batao kya hua.""",
)


@dataclass
class AISettings:
    enabled: bool = True
    name: str = DEFAULT_NAME
    address: str = DEFAULT_ADDRESS
    model: str = DEFAULT_MODEL  # the Gemini model
    mode: str = MODE_CONFIRM
    prompt: str = DEFAULT_PROMPT
    keys_enc: tuple[str, ...] = ()
    ui_mode: str = ""  # "ai" / "manual" as picked on /start; "" = not picked yet (behaves like before)
    prompt_prev: str = ""  # the prompt before the last change, so a change can be undone
    provider: str = DEFAULT_PROVIDER  # who answers: OpenRouter or Google Gemini
    or_model: str = DEFAULT_OR_MODEL  # the OpenRouter model (any model id works)
    or_keys_enc: tuple[str, ...] = ()  # OpenRouter keys, encrypted; the Gemini keys stay in keys_enc

    @classmethod
    def from_dict(cls, data: dict) -> "AISettings":
        mode = data.get("mode")
        provider = data.get("provider")
        if provider not in PROVIDERS:  # saved before OpenRouter existed: Gemini keys mean they were using Google
            provider = PROVIDER_GOOGLE if data.get("keys_enc") else DEFAULT_PROVIDER
        return cls(
            enabled=bool(data.get("enabled", True)),
            name=(data.get("name") or DEFAULT_NAME)[:NAME_MAX],
            address=(data.get("address") or DEFAULT_ADDRESS)[:NAME_MAX],
            model=(data.get("model") or DEFAULT_MODEL).strip().removeprefix("models/"),
            mode=mode if mode in (MODE_CONFIRM, MODE_AUTO) else MODE_CONFIRM,
            prompt=cls._prompt_of(data.get("prompt")),
            keys_enc=tuple(str(k) for k in data.get("keys_enc") or []),
            ui_mode=data.get("ui_mode") if data.get("ui_mode") in (UI_AI, UI_MANUAL) else "",
            prompt_prev=str(data.get("prompt_prev") or "")[:PROMPT_MAX],
            provider=provider,
            or_model=(data.get("or_model") or DEFAULT_OR_MODEL).strip(),
            or_keys_enc=tuple(str(k) for k in data.get("or_keys_enc") or []),
        )

    # --- what belongs to the provider in use ------------------------------------------------------
    @property
    def is_openrouter(self) -> bool:
        return self.provider == PROVIDER_OPENROUTER

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS[self.provider]

    @property
    def active_model(self) -> str:
        return self.or_model if self.is_openrouter else self.model

    @property
    def active_keys_enc(self) -> tuple[str, ...]:
        return self.or_keys_enc if self.is_openrouter else self.keys_enc

    @property
    def keys_field(self) -> str:
        return "or_keys_enc" if self.is_openrouter else "keys_enc"

    @property
    def model_field(self) -> str:
        return "or_model" if self.is_openrouter else "model"

    @property
    def model_presets(self) -> tuple[str, ...]:
        return OR_MODEL_PRESETS if self.is_openrouter else MODEL_PRESETS

    @staticmethod
    def _prompt_of(saved) -> str:
        text = str(saved or "")
        return DEFAULT_PROMPT if not text.strip() or text.strip() in LEGACY_PROMPTS else text[:PROMPT_MAX]

    def system_prompt(self) -> str:
        """The prompt with {name} / {address} filled in. A custom prompt may leave them out."""
        return self.prompt.replace("{name}", self.name).replace("{address}", self.address)


def mask_key(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "…"
