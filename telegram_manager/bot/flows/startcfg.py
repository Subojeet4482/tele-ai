"""Start screen settings: welcome text and image (any admin; the screen is shared by the whole bot)."""
from __future__ import annotations

import io

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import static
from telegram_manager.bot.wizard.spec import Step, WizardContext, WizardSpec
from telegram_manager.constants import START_TEXT_MAX
from telegram_manager.errors import describe_error


async def startcfg_view(store) -> tuple[str, object]:
    """Text and buttons of the start screen settings."""
    cfg = await store.settings.get_start_screen()
    export_on = bool(cfg.get("public_export", True))
    media, kind = store.settings.start_media(cfg)
    has_photo = bool(media)
    custom = (cfg.get("text") or "").strip()
    lines = [
        "🎨 Start screen",
        "",
        "This is what everybody sees on /start.",
        "",
        f"✏️ Welcome text: {'custom' if custom else 'default'}",
        "🖼 Media: " + ({"animation": "GIF", "video": "video"}.get(kind, "photo") if has_photo else "none"),
        "📤 Public session export: "
        + ("ON — anybody can export the session of their own account" if export_on else "OFF"),
    ]
    if custom:
        lines += ["", "“" + (custom if len(custom) <= 200 else custom[:199] + "…") + "”"]
    return "\n".join(lines), keyboards.startcfg_home(export_on, has_photo)


def build_startcfg_specs(deps) -> list[WizardSpec]:
    store = deps.store

    async def parse_text(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if not value:
            raise ValueError("The welcome text cannot be empty.")
        if len(value) > START_TEXT_MAX:
            raise ValueError(f"At most {START_TEXT_MAX} characters (this one has {len(value)}).")
        return value

    async def back_to_screen(wc: WizardContext) -> None:
        text, markup = await startcfg_view(store)
        await wc.show(text, markup)

    async def text_finish(wc: WizardContext) -> None:
        await store.settings.set_start_screen(text=wc.answers["text"])
        await back_to_screen(wc)

    text_spec = WizardSpec(
        name="sc_text",
        title="✏️ Welcome text",
        steps=[
            Step(
                "text",
                static(
                    f"Send the new welcome text (up to {START_TEXT_MAX} characters).\n"
                    "Write {name} where the person's first name should appear.\n"
                    "Plain text and emojis; it is shown on /start to everybody. Admins also see a live status card under it."
                ),
                parse_text,
            )
        ],
        finish=text_finish,
        back_to="sc|home",
    )

    async def parse_photo(wc: WizardContext, raw: str) -> bool:
        if wc.state.media is None:
            raise ValueError("Send a photo, a GIF or a video (not text).")
        return True

    async def photo_finish(wc: WizardContext) -> None:
        kind = wc.state.media_kind or "photo"
        if kind in ("animation", "video"):
            # No download needed: the file id of what the admin sent can be sent again as it is.
            await store.settings.set_start_screen(media=wc.state.media_id, media_type=kind, photo="")
            await back_to_screen(wc)
            return
        data = wc.state.media or b""
        try:
            buffer = io.BytesIO(data)
            buffer.name = "start.jpg"
            # Sending it once gives a real photo file id (an image *file* has none) and doubles as a preview.
            sent = await wc.context.bot.send_photo(wc.update.effective_chat.id, buffer, caption="🖼 New start image")
            photos = getattr(sent, "photo", None)
            file_id = photos[-1].file_id if photos else getattr(sent, "file_id", None)
            if not file_id:
                raise RuntimeError("Telegram did not return the image id")
            await store.settings.set_start_screen(photo=file_id, media=file_id, media_type="photo")
        except Exception as exc:
            await wc.show(f"❌ Could not set the media: {describe_error(exc)}", keyboards.back("sc|home"))
            return
        await back_to_screen(wc)

    photo_spec = WizardSpec(
        name="sc_photo",
        title="🖼 Start screen media",
        steps=[
            Step(
                "photo",
                static("Send a photo, a GIF or a video to show on top of the start screen."),
                parse_photo,
                photo=True,
                media=True,
            )
        ],
        finish=photo_finish,
        back_to="sc|home",
    )
    return [text_spec, photo_spec]
