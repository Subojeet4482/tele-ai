"""Send one message from one of your accounts: account -> chat -> text."""
from __future__ import annotations

import re

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import buttons_step, static, text_step, when_step
from telegram_manager.bot.wizard.spec import Choice, Step, WizardContext, WizardSpec
from telegram_manager.constants import DIALOG_FETCH_LIMIT
from telegram_manager.errors import describe_error
from telegram_manager.parsing import parse_target


def build_message_spec(deps) -> WizardSpec:
    store, manager = deps.store, deps.manager

    async def account_choices(wc: WizardContext) -> list[Choice]:
        rows = await store.accounts.list(wc.user_id)
        return [
            Choice(
                f"{a.get('number')}. {str(a.get('display_name') or 'Unnamed')[:26]} "
                f"{'🟢' if manager.is_online(str(a['id'])) else '🔴'}",
                str(a["id"]),
            )
            for a in rows
        ]

    async def parse_account(wc: WizardContext, raw: str) -> str:
        account = await store.accounts.get(raw.strip(), owner_id=wc.user_id) \
            or await store.accounts.find_by_alias(wc.user_id, raw)
        if account is None:
            raise ValueError("Account not found. Tap one of the buttons.")
        return str(account["id"])

    async def chat_prompt(wc: WizardContext) -> str:
        account = await store.accounts.get(wc.answers["account"], owner_id=wc.user_id)
        label = manager.label_for_account(account) if account else "this account"
        return (
            f"💬 Sending from {label}\n\nPick a chat below, or type a @username / chat id.\n"
            "The chat must already be open in this account."
        )

    async def chat_choices(wc: WizardContext) -> list[Choice]:
        dialogs = await manager.list_dialogs(wc.answers["account"], DIALOG_FETCH_LIMIT, wc.user_id)
        return [Choice(str(d["name"])[:40], str(d["peer_id"])) for d in dialogs]

    async def parse_chat(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if re.fullmatch(r"-?\d{1,20}", value):
            return value  # a chat id: it came from the list below (or was typed)
        return parse_target(value)

    steps = [
        Step("account", static("💬 Choose the account to send from."), parse_account, account_choices),
        Step("chat", chat_prompt, parse_chat, chat_choices, paged=True),
        text_step("✉️ Send the message text."),
        buttons_step(),
        when_step(),
    ]

    async def finish(wc: WizardContext) -> None:
        answers = wc.answers
        if answers.get("when"):  # a timed message goes through the runner: Stop cancels it
            await deps.bulk.launch(
                wc.update,
                wc.context,
                wc.user_id,
                "one",
                answers["chat"],
                answers["text"],
                0,
                account_ids=[answers["account"]],
                start_in=answers["when"] * 60,
                buttons=answers.get("buttons") or None,
            )
            return
        try:
            extra = {"buttons": answers["buttons"]} if answers.get("buttons") else {}
            await manager.send_to_existing_dialog(
                answers["account"], answers["chat"], answers["text"], wc.user_id, **extra
            )
        except Exception as exc:
            await wc.show(
                f"❌ Could not send: {describe_error(exc)}", keyboards.after_action("🔁 Try again", "msg|start")
            )
            return
        await wc.show("✅ Message sent.", keyboards.after_action("💬 Send another", "msg|start"))

    return WizardSpec(name="message", title="💬 Message from one account", steps=steps, finish=finish, back_to="menu")
