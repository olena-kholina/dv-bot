"""End-to-end test: drives the real handlers with a fake Telegram API (no network, no token).

Run:  python test_bot.py
"""
import asyncio
import json
import os
import tempfile
import time

os.environ.update(ADMIN_IDS="1", COMMUNITY_CHAT_ID="-1001234567890", FOLLOWUP_DAYS="3", WELCOME_IN_GROUP="1")

from telegram import Update  # noqa: E402
from telegram.request import BaseRequest  # noqa: E402

import bot  # noqa: E402
from db import DB  # noqa: E402

GROUP = -1001234567890
ADMIN, ALICE, BOB, CAROL = 1, 101, 102, 103


class FakeTelegram(BaseRequest):
    """Answers Bot API calls and records every sendMessage/edit."""

    def __init__(self):
        self.sent, self.edits, self.answers, self.msg_id = [], [], [], 1000

    async def initialize(self): pass

    async def shutdown(self): pass

    @property
    def read_timeout(self): return 5

    async def do_request(self, url, method, request_data=None, **kw):
        api = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if api == "getMe":
            r = {"id": 999, "is_bot": True, "first_name": "DVC", "username": "dvc_bot"}
        elif api == "sendMessage":
            self.msg_id += 1
            chat = int(params["chat_id"])
            markup = params.get("reply_markup")
            if isinstance(markup, str):
                markup = json.loads(markup)
            self.sent.append({"chat": chat, "text": params["text"], "markup": markup, "id": self.msg_id})
            r = {"message_id": self.msg_id, "date": int(time.time()), "text": params["text"],
                 "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"}}
        elif api in ("editMessageText", "editMessageReplyMarkup"):
            self.edits.append((api, params))
            r = True
        elif api == "answerCallbackQuery":
            self.answers.append(params.get("text"))
            r = True
        elif api == "getChatMember":
            uid = int(params["user_id"])
            status = "left" if uid == 666 else "member"
            r = {"status": status, "user": {"id": uid, "is_bot": False, "first_name": "x"}}
        else:
            r = True
        return 200, json.dumps({"ok": True, "result": r}).encode()


_uid = [0]


def _next():
    _uid[0] += 1
    return _uid[0]


def user(uid, name, username=None):
    u = {"id": uid, "is_bot": False, "first_name": name}
    if username:
        u["username"] = username
    return u


USERS = {ADMIN: user(ADMIN, "Olena", "olena"), ALICE: user(ALICE, "Alice", "alice"),
         BOB: user(BOB, "Bob"), CAROL: user(CAROL, "Carol", "carol"), 666: user(666, "Stranger")}


def text_update(uid, text, chat=None):
    chat = chat or {"id": uid, "type": "private"}
    msg = {"message_id": _next(), "date": int(time.time()), "chat": chat, "from": USERS[uid], "text": text}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": _next(), "message": msg}


def tap(uid, data, chat_id=None, message_text="card"):
    chat_id = chat_id or uid
    msg = {"message_id": _next(), "date": int(time.time()), "text": message_text,
           "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private"},
           "from": {"id": 999, "is_bot": True, "first_name": "DVC"}}
    return {"update_id": _next(), "callback_query": {"id": str(_next()), "from": USERS[uid],
                                                      "chat_instance": "x", "data": data, "message": msg}}


async def main():
    tmp = tempfile.mkdtemp()
    bot.db = DB(os.path.join(tmp, "test.db"))
    fake = FakeTelegram()
    app = bot.build_app("123:TEST", request=fake)
    await app.initialize()

    async def send(raw):
        await app.process_update(Update.de_json(raw, app.bot))

    def last(chat):
        return [m for m in fake.sent if m["chat"] == chat][-1]

    def buttons(m):
        return [b["callback_data"] for row in (m["markup"] or {}).get("inline_keyboard", []) for b in row
                if "callback_data" in b]

    async def onboard(uid, name, company, role, expertise, offers, findable=True, intro="send"):
        await send(text_update(uid, "/start"))
        assert "1/7" in last(uid)["text"], last(uid)["text"]
        for answer in (name, company):
            await send(text_update(uid, answer))
        await send(tap(uid, f"role:{role}"))
        for answer in (expertise, offers, "AI, fundraising", "/skip"):
            await send(text_update(uid, answer))
        await send(tap(uid, "srch:yes" if findable else "srch:no"))
        await send(tap(uid, f"intro:{intro}"))

    # --- non-members are turned away
    await send(text_update(666, "/start"))
    assert "for members of" in last(666)["text"]

    # --- 1. onboarding: profile + intro draft -> admin approval -> posted
    await onboard(ALICE, "Alice Chen", "MedFlow", "Founder", "Healthcare SaaS", "fundraising advice")
    assert bot.db.get_member(ALICE)["intro_status"] == "pending"
    card = last(ADMIN)
    assert "Intro #1" in card["text"] and "Alice Chen" in card["text"], card["text"]
    await send(tap(ADMIN, "adm:s:1:ok", message_text=card["text"]))
    assert "New member" in last(GROUP)["text"] and "Alice Chen" in last(GROUP)["text"]
    assert "intro is live" in last(ALICE)["text"]
    assert bot.db.get_member(ALICE)["intro_status"] == "posted"

    await onboard(BOB, "Bob Ivanov", "CareSales", "Operator",
                  "Sold to US hospitals, hired first US enterprise salesperson", "enterprise sales hiring", intro="skip")
    await onboard(CAROL, "Carol Diaz", "Seed Fund", "Investor", "climate investing", "deck reviews", findable=False,
                  intro="skip")

    # --- 2. find members: matches Bob, never shows hidden Carol
    await send(text_update(ALICE, "/find who has sold to hospitals?"))
    res = last(ALICE)["text"]
    assert "Bob Ivanov" in res and "Carol" not in res, res
    await send(text_update(ALICE, "/find climate"))
    assert "No matching profiles" in last(ALICE)["text"]
    await send(tap(ALICE, "menu:find"))
    await send(text_update(ALICE, "enterprise sales"))
    assert "Bob Ivanov" in last(ALICE)["text"]

    # --- 3. ask the community: nudge, preview, admin approval, post with buttons
    await send(tap(ALICE, "menu:ask"))
    await send(text_update(ALICE, "Need a sales hire"))
    assert "add a little context" in last(ALICE)["text"]
    await send(text_update(ALICE, "Looking for someone who has hired their first US enterprise salesperson"))
    await send(text_update(ALICE, "A founder who sold into US hospitals"))
    await send(tap(ALICE, "dl:1"))
    assert "Within 2 weeks" in last(ALICE)["text"] and "ask:send" in buttons(last(ALICE))
    await send(tap(ALICE, "ask:send"))
    assert bot.db.get_ask(1)["status"] == "pending"
    card = last(ADMIN)
    assert "Ask #1" in card["text"]

    # non-admin can't approve
    await send(tap(ALICE, "adm:a:1:ok"))
    assert fake.answers[-1] == "Admins only."
    await send(tap(ADMIN, "adm:a:1:ok", message_text=card["text"]))
    post = last(GROUP)
    assert "Ask from Alice Chen" in post["text"] and buttons(post) == ["help:1", "same:1"]
    live_dm = last(ALICE)["text"]
    assert "ask is live" in live_dm and "Bob Ivanov" in live_dm, live_dm  # suggested match
    # double approval is blocked
    await send(tap(ADMIN, "adm:a:1:ok"))
    assert fake.answers[-1] == "Already handled."

    # --- members respond in the group
    await send(tap(ALICE, "help:1", chat_id=GROUP))
    assert "your ask" in fake.answers[-1]
    await send(tap(BOB, "help:1", chat_id=GROUP))
    assert "can help with your ask" in last(ALICE)["text"] and "Bob Ivanov" in last(ALICE)["text"]
    n = len([m for m in fake.sent if m["chat"] == ALICE])
    await send(tap(BOB, "help:1", chat_id=GROUP))  # repeat tap: no second DM
    assert len([m for m in fake.sent if m["chat"] == ALICE]) == n
    await send(tap(CAROL, "same:1", chat_id=GROUP))
    assert bot.db.response_counts(1) == {"help": 1, "same": 1}

    # --- follow-up after N days: "not yet" keeps it open, "yes" closes and marks the post resolved
    bot.db._run("UPDATE asks SET approved_at=approved_at-4*86400 WHERE id=1")
    await bot.followup_job(type("Ctx", (), {"bot": app.bot})())
    fu = last(ALICE)
    assert "Checking in" in fu["text"] and "fu:1:open" in buttons(fu)
    await send(tap(ALICE, "fu:1:open"))
    assert bot.db.get_ask(1)["status"] == "live"
    await bot.followup_job(type("Ctx", (), {"bot": app.bot})())  # not due again yet
    assert last(ALICE)["text"] != fu["text"] or len(bot.db.due_followups(3 * 86400)) == 0
    await send(tap(BOB, "fu:1:helped"))  # only the requester can close
    assert bot.db.get_ask(1)["status"] == "live"
    await send(tap(ALICE, "fu:1:helped"))
    assert bot.db.get_ask(1)["outcome"] == "helped"
    assert any(api == "editMessageText" and "Resolved" in p.get("text", "") for api, p in fake.edits)

    # --- my asks + manual close
    await send(text_update(BOB, "/ask"))
    await send(text_update(BOB, "Looking for intros to hospital procurement leads in Germany and Austria"))
    await send(text_update(BOB, "Someone selling medtech in DACH"))
    await send(tap(BOB, "dl:3"))
    await send(tap(BOB, "ask:send"))
    await send(tap(ADMIN, "adm:a:2:ok"))
    await send(text_update(BOB, "/myasks"))
    assert "close:2" in buttons(last(BOB))
    await send(tap(BOB, "close:2"))
    await send(tap(BOB, "fu:2:unresolved"))
    assert bot.db.get_ask(2)["outcome"] == "unresolved"

    # --- 4. discussion suggestions: anonymous, admin approves
    await send(tap(CAROL, "menu:topic"))
    await send(text_update(CAROL, "How are you budgeting for AI tooling in 2027?"))
    await send(tap(CAROL, "anon:1"))
    card = last(ADMIN)
    assert "Discussion suggestion (anonymous)" in card["text"]
    sub_id = bot.db.pending_submissions()[0]["id"]
    await send(tap(ADMIN, f"adm:s:{sub_id}:ok"))
    assert "Discussion" in last(GROUP)["text"] and "a member" in last(GROUP)["text"]
    assert "Carol" not in last(GROUP)["text"]

    # --- switching flows mid-way doesn't leave stale state
    await send(tap(ALICE, "menu:ask"))
    await send(tap(ALICE, "menu:find"))
    await send(text_update(ALICE, "hospitals"))
    assert "Bob Ivanov" in last(ALICE)["text"]
    await send(text_update(ALICE, "/cancel"))

    # --- group welcome + admin tools
    join = text_update(ALICE, "", chat={"id": GROUP, "type": "supergroup"})
    del join["message"]["text"]
    join["message"]["new_chat_members"] = [USERS[666]]
    await send(join)
    assert "Welcome" in last(GROUP)["text"]
    await send(text_update(ADMIN, "/stats"))
    stats = last(ADMIN)["text"]
    assert "Profiles: 3" in stats and "1 helped" in stats, stats
    await send(text_update(ADMIN, "/pending"))
    assert "Nothing waiting" in last(ADMIN)["text"]
    await send(text_update(ALICE, "/stats"))  # non-admin: ignored
    assert "Community measures" not in last(ALICE)["text"]

    await app.shutdown()
    print("All flows passed ✅")
    print("\n--- Sample stats ---\n" + stats)


if __name__ == "__main__":
    asyncio.run(main())
