"""Change an account's name, bio or profile photo (started from the account screen)."""
from __future__ import annotations

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import static
from telegram_manager.bot.wizard.spec import Choice, Step, WizardContext, WizardSpec
from telegram_manager.core.accountctl import parse_username
from telegram_manager.core.profile import MAX_BIO, MAX_NAME
from telegram_manager.errors import describe_error


def build_profile_specs(deps) -> list[WizardSpec]:
    manager, store = deps.manager, deps.store

    async def current_name(wc: WizardContext) -> str:
        account = await store.accounts.get(wc.answers["account"], owner_id=wc.user_id)
        return str(account.get("display_name") or "Unnamed") if account else "this account"

    async def done(wc: WizardContext, text: str) -> None:
        await wc.show(text, keyboards.back(f"acc|{wc.answers['account']}", "⬅️ Back to account"))

    async def failed(wc: WizardContext, exc: Exception) -> None:
        await done(wc, f"❌ Could not change it: {describe_error(exc)}")

    def limited(label: str, limit: int, allow_empty: bool):
        async def parse(wc: WizardContext, raw: str) -> str:
            value = raw.strip()
            if value == "-" and allow_empty:
                return ""
            if not value:
                raise ValueError(f"The {label} cannot be empty.")
            if len(value) > limit:
                raise ValueError(f"The {label} can have at most {limit} characters (this one has {len(value)}).")
            return value

        return parse

    # --- name -------------------------------------------------------------------------------
    async def first_prompt(wc: WizardContext) -> str:
        return f"✏️ Now: {await current_name(wc)}\n\nSend the new first name (up to {MAX_NAME} characters)."

    async def name_finish(wc: WizardContext) -> None:
        try:
            name = await manager.update_profile(
                wc.answers["account"], wc.user_id, first_name=wc.answers["first"], last_name=wc.answers["last"]
            )
        except Exception as exc:
            await failed(wc, exc)
            return
        await done(wc, f"✅ Name changed to {name or wc.answers['first']}.")

    name_spec = WizardSpec(
        name="pf_name",
        title="✏️ Change name",
        steps=[
            Step("first", first_prompt, limited("first name", MAX_NAME, False)),
            Step(
                "last",
                static(f"Send the new last name, or - to leave it empty (up to {MAX_NAME} characters)."),
                limited("last name", MAX_NAME, True),
            ),
        ],
        finish=name_finish,
        back_to="acc_list",
    )

    # --- bio ----------------------------------------------------------------------------------
    async def bio_finish(wc: WizardContext) -> None:
        try:
            await manager.update_profile(wc.answers["account"], wc.user_id, about=wc.answers["about"])
        except Exception as exc:
            await failed(wc, exc)
            return
        await done(wc, "✅ Bio cleared." if not wc.answers["about"] else "✅ Bio changed.")

    bio_spec = WizardSpec(
        name="pf_bio",
        title="📝 Change bio",
        steps=[
            Step(
                "about",
                static(f"Send the new bio (up to {MAX_BIO} characters), or - to clear it."),
                limited("bio", MAX_BIO, True),
            )
        ],
        finish=bio_finish,
        back_to="acc_list",
    )

    # --- photo --------------------------------------------------------------------------------
    async def parse_photo(wc: WizardContext, raw: str) -> bool:
        if wc.state.media is None:
            raise ValueError("Send a photo (as a picture or an image file), not text.")
        return True

    async def photo_finish(wc: WizardContext) -> None:
        try:
            await manager.set_profile_photo(wc.answers["account"], wc.user_id, wc.state.media or b"")
        except Exception as exc:
            await failed(wc, exc)
            return
        await done(wc, "✅ Profile photo changed.")

    photo_spec = WizardSpec(
        name="pf_photo",
        title="🖼 Change profile photo",
        steps=[Step("photo", static("Send the new profile photo."), parse_photo, photo=True)],
        finish=photo_finish,
        back_to="acc_list",
    )
    # --- username -----------------------------------------------------------------------------
    async def username_prompt(wc: WizardContext) -> str:
        account = await store.accounts.get(wc.answers["account"], owner_id=wc.user_id)
        now = f"@{account['username']}" if account and account.get("username") else "none"
        return (
            f"🔤 Username now: {now}\n\nSend the new username (5–32 characters: letters, digits, _), "
            "or - to remove it."
        )

    async def parse_name(wc: WizardContext, raw: str) -> str:
        value = parse_username(raw)  # "" = remove
        return value or "-"

    async def username_choices(wc: WizardContext):
        return [Choice("🗑 Remove username", "-")]

    async def username_finish(wc: WizardContext) -> None:
        new = "" if wc.answers["username"] == "-" else wc.answers["username"]
        try:
            await manager.set_username(wc.answers["account"], wc.user_id, new)
        except Exception as exc:
            await failed(wc, exc)
            return
        await done(wc, "✅ Username removed." if not new else f"✅ Username is now @{new}.")

    username_spec = WizardSpec(
        name="pf_username",
        title="🔤 Username",
        steps=[Step("username", username_prompt, parse_name, username_choices)],
        finish=username_finish,
        back_to="acc_list",
    )
    return [name_spec, bio_spec, photo_spec, username_spec]
