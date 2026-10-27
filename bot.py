"""DVC community bot: Ask the community, Find members, Onboarding, Discussion suggestions.

Everything that gets posted to the community passes through admin approval first.
"""
import html
import logging
import os
import time
import warnings

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as M
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, Defaults, MessageHandler, filters)
from telegram.warnings import PTBUserWarning

from db import DB

warnings.filterwarnings("ignore", category=PTBUserWarning)
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("dvc-bot")

load_dotenv()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
COMMUNITY_CHAT_ID = int(os.getenv("COMMUNITY_CHAT_ID", "0") or 0) or None
COMMUNITY_THREAD_ID = int(os.getenv("COMMUNITY_THREAD_ID", "0") or 0) or None
COMMUNITY_NAME = os.getenv("COMMUNITY_NAME", "DVC")
FOLLOWUP_DAYS = float(os.getenv("FOLLOWUP_DAYS", "3"))
WELCOME_IN_GROUP = os.getenv("WELCOME_IN_GROUP", "1") == "1"
DB_PATH = os.getenv("DB_PATH", "dvc.db")

db: DB | None = None
END = ConversationHandler.END

# Conversation states
(O_NAME, O_COMPANY, O_ROLE, O_EXPERTISE, O_OFFERS, O_INTERESTS, O_LOOKING, O_SEARCHABLE,
 O_INTRO, O_INTRO_EDIT, A_NEED, A_WHO, A_DEADLINE, A_CONFIRM, F_QUERY, T_TEXT, T_ANON) = range(17)

ROLES = ["Founder", "Investor", "Operator", "Other"]
DEADLINES = ["This week", "Within 2 weeks", "This month", "No rush"]


# ---------------------------------------------------------------- helpers
def e(s) -> str:
    return html.escape(str(s or ""))


def mention(user_id, name, username=None) -> str:
    if username:
        return f"@{e(username)}"
    return f'<a href="tg://user?id={user_id}">{e(name) or "member"}</a>'


def member_mention(m) -> str:
    return mention(m["user_id"], m["name"], m["username"])


def main_menu() -> M:
    return M([
        [B("🙋 Ask the community", callback_data="menu:ask")],
        [B("🤝 Find members", callback_data="menu:find")],
        [B("💬 Suggest a discussion", callback_data="menu:topic")],
        [B("👤 My profile", callback_data="menu:profile"), B("📋 My asks", callback_data="menu:myasks")],
    ])


async def reply(update: Update, text: str, markup=None):
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except BadRequest:
            pass
    return await update.effective_message.reply_text(text, reply_markup=markup)


async def safe_dm(ctx, user_id, text, markup=None) -> bool:
    """DM a user; members who never opened the bot can't be messaged."""
    try:
        await ctx.bot.send_message(user_id, text, reply_markup=markup)
        return True
    except (Forbidden, BadRequest) as err:
        log.info("Could not DM %s: %s", user_id, err)
        return False


async def post_to_community(ctx, text, markup=None):
    return await ctx.bot.send_message(COMMUNITY_CHAT_ID, text, reply_markup=markup,
                                      message_thread_id=COMMUNITY_THREAD_ID)


async def notify_admins(ctx, text, markup):
    for admin_id in ADMIN_IDS:
        await safe_dm(ctx, admin_id, text, markup)


async def gate(update: Update, ctx) -> bool:
    """Only members of the community group can use the bot (admins always can)."""
    uid = update.effective_user.id
    if uid in ADMIN_IDS or not COMMUNITY_CHAT_ID:
        return True
    if ctx.user_data.get("_member_until", 0) > time.time():
        return True
    try:
        cm = await ctx.bot.get_chat_member(COMMUNITY_CHAT_ID, uid)
        ok = cm.status in ("member", "administrator", "creator") or (
            cm.status == "restricted" and getattr(cm, "is_member", False))
    except TelegramError:
        ok = False
    if ok:
        ctx.user_data["_member_until"] = time.time() + 3600
        return True
    await reply(update, f"This bot is for members of the {e(COMMUNITY_NAME)} community. "
                        "If you've just joined, try again in a minute, or contact the team.")
    return False


def clear_flow(ctx):
    for key in ("p", "ask", "topic", "nudged"):
        ctx.user_data.pop(key, None)


def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


# ---------------------------------------------------------------- /start, menu, profile
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return END
    m = db.get_member(update.effective_user.id)
    if not m or (ctx.args and ctx.args[0] == "onboard"):
        return await onb_begin(update, ctx)
    await reply(update, f"Hi {e(m['name'])}! What would you like to do?", main_menu())
    return END


async def help_cmd(update: Update, ctx):
    text = (f"<b>{e(COMMUNITY_NAME)} community bot</b>\n\n"
            "🙋 /ask: post a specific request to the community\n"
            "🤝 /find: search member profiles, e.g. <i>/find sold to hospitals</i>\n"
            "💬 /topic: suggest a discussion question\n"
            "👤 /profile: view or update your profile\n"
            "📋 /myasks: see and close your asks\n"
            "/cancel stops whatever you're doing.")
    if is_admin(update):
        text += "\n\n<b>Admin</b>\n/pending: review the approval queue\n/stats: community measures"
    await reply(update, text, main_menu())


async def show_profile(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    m = db.get_member(update.effective_user.id)
    if not m:
        await reply(update, "You don't have a profile yet.",
                    M([[B("Set up my profile", callback_data="menu:onboard")]]))
        return END
    vis = "visible in search ✅" if m["searchable"] else "hidden from search 🙈"
    text = (f"<b>{e(m['name'])}</b> · {e(m['role'])}, {e(m['company'])}\n\n"
            f"<b>Knows:</b> {e(m['expertise'])}\n<b>Can help with:</b> {e(m['offers'])}\n"
            f"<b>Interested in:</b> {e(m['interests'])}\n<b>Looking for:</b> {e(m['looking_for']) or '—'}\n\n"
            f"Your profile is {vis}.")
    toggle = "🙈 Hide me from search" if m["searchable"] else "✅ Show me in search"
    await reply(update, text, M([[B("✏️ Update profile", callback_data="menu:onboard")],
                                 [B(toggle, callback_data="prof:toggle")]]))
    return END


async def toggle_search(update: Update, ctx):
    q = update.callback_query
    m = db.get_member(q.from_user.id)
    if not m:
        await q.answer()
        return
    db.set_member_field(m["user_id"], "searchable", 0 if m["searchable"] else 1)
    await q.answer("Hidden from search" if m["searchable"] else "You're now visible in search")
    await show_profile(update, ctx)


# ---------------------------------------------------------------- onboarding
async def onb_begin(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    clear_flow(ctx)
    ctx.user_data["p"] = {}
    existing = db.get_member(update.effective_user.id)
    intro = (f"Welcome to {e(COMMUNITY_NAME)} 👋\n\nLet's set up your member profile: 7 quick questions, "
             "about 2 minutes. It helps others find you and helps the bot match you with asks.\n"
             "Type /cancel anytime.\n\n" if not existing else "Let's update your profile.\n\n")
    await reply(update, intro + "<b>1/7</b> What's your name?")
    return O_NAME


async def onb_name(update: Update, ctx):
    ctx.user_data["p"]["name"] = update.message.text.strip()
    await update.message.reply_text("<b>2/7</b> Company or fund?")
    return O_COMPANY


async def onb_company(update: Update, ctx):
    ctx.user_data["p"]["company"] = update.message.text.strip()
    await update.message.reply_text("<b>3/7</b> Which best describes you?",
                                    reply_markup=M([[B(r, callback_data=f"role:{r}") for r in ROLES]]))
    return O_ROLE


async def onb_role(update: Update, ctx):
    ctx.user_data["p"]["role"] = update.callback_query.data.split(":", 1)[1]
    await reply(update, "<b>4/7</b> What do you know deeply?\n"
                        "<i>e.g. B2B SaaS pricing, selling to US hospitals, hiring engineers in Poland</i>")
    return O_EXPERTISE


async def onb_expertise(update: Update, ctx):
    ctx.user_data["p"]["expertise"] = update.message.text.strip()
    await update.message.reply_text("<b>5/7</b> What could you help other members with?\n"
                                    "<i>e.g. intros to EU healthcare buyers, reviewing pitch decks, fundraising advice</i>")
    return O_OFFERS


async def onb_offers(update: Update, ctx):
    ctx.user_data["p"]["offers"] = update.message.text.strip()
    await update.message.reply_text("<b>6/7</b> Which topics would you like to learn about or discuss?")
    return O_INTERESTS


async def onb_interests(update: Update, ctx):
    ctx.user_data["p"]["interests"] = update.message.text.strip()
    await update.message.reply_text("<b>7/7</b> What's one thing you're looking for right now? "
                                    "<i>(a hire, an intro, advice…)</i>\nOr send /skip.")
    return O_LOOKING


async def onb_looking(update: Update, ctx):
    text = update.message.text.strip()
    ctx.user_data["p"]["looking_for"] = "" if text.startswith("/skip") else text
    await update.message.reply_text(
        "Last thing: can other members find you in search, and can the bot suggest you when "
        "someone asks for help in your area? You can change this anytime in /profile.",
        reply_markup=M([[B("✅ Yes, make me findable", callback_data="srch:yes")],
                        [B("🙈 No, keep me hidden", callback_data="srch:no")]]))
    return O_SEARCHABLE


def draft_intro(p) -> str:
    lines = [f"Hi everyone, I'm {p['name']}, {p['role'].lower()} at {p['company']}."]
    if p.get("expertise"):
        lines.append(f"I know a lot about {p['expertise']}.")
    if p.get("offers"):
        lines.append(f"Happy to help with: {p['offers']}.")
    if p.get("looking_for"):
        lines.append(f"Right now I'm looking for: {p['looking_for']}.")
    lines.append("Say hi or send me a message!")
    return "\n".join(line.replace("..", ".") for line in lines)


async def onb_searchable(update: Update, ctx):
    user = update.effective_user
    p = ctx.user_data["p"]
    p["searchable"] = 1 if update.callback_query.data == "srch:yes" else 0
    db.upsert_member(user.id, user.username, **p)
    m = db.get_member(user.id)
    if m["intro_status"] in ("posted", "pending"):
        await reply(update, "✅ Profile saved.", main_menu())
        clear_flow(ctx)
        return END
    p["intro"] = draft_intro(p)
    await reply(update, "✅ Profile saved.\n\nNow let's introduce you to the community. Here's a draft:\n\n"
                        f"<blockquote>{e(p['intro'])}</blockquote>",
                M([[B("✅ Submit this intro", callback_data="intro:send")],
                   [B("✏️ Write my own", callback_data="intro:edit")],
                   [B("Skip for now", callback_data="intro:skip")]]))
    return O_INTRO


async def onb_intro_choice(update: Update, ctx):
    choice = update.callback_query.data.split(":")[1]
    if choice == "edit":
        await reply(update, "Send me your intro text. A good intro has one ask and one offer.")
        return O_INTRO_EDIT
    if choice == "skip":
        await reply(update, "No problem. You can post an intro later from /profile → Update profile.", main_menu())
        clear_flow(ctx)
        return END
    return await submit_intro(update, ctx, ctx.user_data["p"]["intro"])


async def onb_intro_edit(update: Update, ctx):
    return await submit_intro(update, ctx, update.message.text.strip())


async def submit_intro(update: Update, ctx, text):
    user = update.effective_user
    sub_id = db.create_submission("intro", user.id, text)
    db.set_member_field(user.id, "intro_status", "pending")
    await send_submission_card(ctx, db.get_submission(sub_id))
    await reply(update, "🎉 Thanks! Your intro is with the team and will be posted shortly.\n\n"
                        "While you wait, here's what you can do:", main_menu())
    clear_flow(ctx)
    return END


async def welcome_new_members(update: Update, ctx):
    if not WELCOME_IN_GROUP or update.effective_chat.id != COMMUNITY_CHAT_ID:
        return
    people = [u for u in update.message.new_chat_members if not u.is_bot]
    if not people:
        return
    names = ", ".join(mention(u.id, u.first_name, u.username) for u in people)
    link = f"https://t.me/{ctx.bot.username}?start=onboard"
    await update.message.reply_text(
        f"Welcome {names}! 👋 Set up your profile and intro in 2 minutes. It's how members find you.",
        reply_markup=M([[B("Set up my profile", url=link)]]))


# ---------------------------------------------------------------- ask the community
def ask_post_text(ask, m, resolved=False) -> str:
    text = (f"🙋 <b>Ask from {e(m['name'])}</b> ({e(m['role'])}, {e(m['company'])})\n\n"
            f"{e(ask['need'])}\n\n<b>Ideal helper:</b> {e(ask['who'])}\n<b>Timing:</b> {e(ask['deadline'])}")
    if resolved:
        return text + "\n\n✅ <b>Resolved</b>, thanks to everyone who helped."
    return text + f"\n\nCan you help? Tap below and {e(m['name'].split()[0])} will get a private note."


def ask_buttons(ask_id, counts) -> M:
    help_label = "🤝 I can help" + (f" ({counts['help']})" if counts["help"] else "")
    same_label = "✋ Same question" + (f" ({counts['same']})" if counts["same"] else "")
    return M([[B(help_label, callback_data=f"help:{ask_id}"), B(same_label, callback_data=f"same:{ask_id}")]])


async def ask_begin(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    clear_flow(ctx)
    if not db.get_member(update.effective_user.id):
        await reply(update, "Before posting an ask, please set up a quick profile so members know who's asking.",
                    M([[B("Set up my profile", callback_data="menu:onboard")]]))
        return END
    ctx.user_data["ask"] = {}
    await reply(update, "🙋 <b>Ask the community</b>\n\n<b>1/3</b> What do you need? The more specific, "
                        "the more likely someone can help.\n\n<i>e.g. Looking for someone who has hired their "
                        "first US enterprise salesperson: how did you structure comp and the first 90 days?</i>")
    return A_NEED


async def ask_need(update: Update, ctx):
    text = update.message.text.strip()
    if len(text) < 40 and not ctx.user_data.get("nudged"):
        ctx.user_data["nudged"] = True
        await update.message.reply_text("Could you add a little context? Your stage, market, or what you've "
                                        "already tried helps people decide if they can help.\n"
                                        "Send a fuller version, or send the same text again to keep it.")
        return A_NEED
    ctx.user_data["ask"]["need"] = text
    await update.message.reply_text("<b>2/3</b> Who would be the ideal person to help?\n"
                                    "<i>e.g. a founder who has sold into US hospitals; a seed investor in climate</i>")
    return A_WHO


async def ask_who(update: Update, ctx):
    ctx.user_data["ask"]["who"] = update.message.text.strip()
    await update.message.reply_text("<b>3/3</b> When do you need it by?",
                                    reply_markup=M([[B(d, callback_data=f"dl:{i}")] for i, d in enumerate(DEADLINES)]))
    return A_DEADLINE


async def ask_deadline(update: Update, ctx):
    ask = ctx.user_data["ask"]
    ask["deadline"] = DEADLINES[int(update.callback_query.data.split(":")[1])]
    m = db.get_member(update.effective_user.id)
    await reply(update, "Here's how your post will look:\n\n" + ask_post_text(ask, m),
                M([[B("✅ Submit for posting", callback_data="ask:send")],
                   [B("↩️ Start over", callback_data="ask:redo"), B("✖️ Cancel", callback_data="ask:cancel")]]))
    return A_CONFIRM


async def ask_confirm(update: Update, ctx):
    action = update.callback_query.data.split(":")[1]
    if action == "redo":
        return await ask_begin(update, ctx)
    if action == "cancel":
        clear_flow(ctx)
        await reply(update, "Cancelled.", main_menu())
        return END
    a = ctx.user_data["ask"]
    uid = update.effective_user.id
    ask_id = db.create_ask(uid, a["need"], a["who"], a["deadline"])
    await send_ask_card(ctx, db.get_ask(ask_id))
    clear_flow(ctx)
    await reply(update, "📨 Sent to the team for a quick review. I'll message you when it's live, "
                        "along with members whose profiles match.", main_menu())
    return END


async def ask_response_tap(update: Update, ctx):
    """'I can help' / 'Same question' buttons on a community post."""
    q = update.callback_query
    kind, ask_id = q.data.split(":")
    ask = db.get_ask(int(ask_id))
    if not ask or ask["status"] != "live":
        await q.answer("This ask has been closed.", show_alert=True)
        return
    user = q.from_user
    if user.id == ask["user_id"]:
        await q.answer("This is your ask 🙂", show_alert=True)
        return
    is_new = db.add_response(ask["id"], user.id, kind)
    requester = db.get_member(ask["user_id"])
    helper = db.get_member(user.id)
    helper_label = mention(user.id, helper["name"] if helper else user.full_name, user.username)
    if kind == "help":
        if is_new:
            about = ""
            if helper:
                about = f"\n<i>{e(helper['role'])}, {e(helper['company'])}. Knows: {e(helper['expertise'])}</i>"
            await safe_dm(ctx, ask["user_id"],
                          f"🤝 {helper_label} says they can help with your ask:\n“{e(ask['need'][:120])}”{about}\n\n"
                          "Send them a message to connect.")
        await q.answer(f"Thanks! {requester['name'].split()[0]} has been told you can help." if is_new
                       else "You've already offered. Thank you!", show_alert=is_new)
    else:
        await q.answer("Noted. Follow the replies to this post." if is_new else "Already noted.")
    if is_new:
        try:
            await q.edit_message_reply_markup(ask_buttons(ask["id"], db.response_counts(ask["id"])))
        except BadRequest:
            pass


async def my_asks(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    asks = db.asks_for_user(update.effective_user.id)
    if not asks:
        await reply(update, "You haven't posted any asks yet.", main_menu())
        return END
    labels = {"pending": "⏳ in review", "live": "🟢 live", "rejected": "not posted",
              "closed": "✅ closed"}
    lines, buttons = [], []
    for a in asks:
        c = db.response_counts(a["id"])
        lines.append(f"#{a['id']} · {labels.get(a['status'], a['status'])} · {c['help']} offers\n"
                     f"<i>{e(a['need'][:90])}</i>")
        if a["status"] == "live":
            buttons.append([B(f"Close #{a['id']}", callback_data=f"close:{a['id']}")])
    await reply(update, "📋 <b>Your asks</b>\n\n" + "\n\n".join(lines), M(buttons) if buttons else None)
    return END


def outcome_buttons(ask_id, include_not_yet=True) -> M:
    rows = [[B("✅ Yes, I got help", callback_data=f"fu:{ask_id}:helped")],
            [B("🟡 Partly", callback_data=f"fu:{ask_id}:partial")]]
    if include_not_yet:
        rows.append([B("⏳ Not yet, keep it open", callback_data=f"fu:{ask_id}:open")])
    else:
        rows.append([B("❌ No, close it anyway", callback_data=f"fu:{ask_id}:unresolved")])
    return M(rows)


async def close_prompt(update: Update, ctx):
    q = update.callback_query
    ask = db.get_ask(int(q.data.split(":")[1]))
    if not ask or ask["user_id"] != q.from_user.id:
        await q.answer()
        return
    await reply(update, f"Did you get the help you needed with #{ask['id']}?", outcome_buttons(ask["id"], False))


async def followup_answer(update: Update, ctx):
    q = update.callback_query
    _, ask_id, outcome = q.data.split(":")
    ask = db.get_ask(int(ask_id))
    if not ask or ask["user_id"] != q.from_user.id:
        await q.answer()
        return
    await q.answer()
    if ask["status"] != "live":
        await q.edit_message_text("This ask is already closed. Thanks!")
        return
    if outcome == "open":
        more = (f"I'll check in again in {FOLLOWUP_DAYS:g} days." if ask["followups"] < 2
                else "You can close it anytime from 📋 My asks.")
        await q.edit_message_text(f"Got it, it stays open. {more}\n\nTip: replying in the thread with "
                                  "an update or a sharper question often brings new answers.")
        return
    db.close_ask(ask["id"], outcome)
    requester = db.get_member(ask["user_id"])
    try:
        await ctx.bot.edit_message_text(ask_post_text(ask, requester, resolved=True),
                                        chat_id=ask["chat_id"], message_id=ask["message_id"])
    except TelegramError as err:
        log.info("Could not mark ask %s resolved: %s", ask["id"], err)
    thanks = {"helped": "🎉 Great to hear! I've marked the post as resolved.",
              "partial": "Thanks. Closed and noted as partly helped.",
              "unresolved": "Thanks for letting us know. Closed. The team will see this and may follow up."}
    await q.edit_message_text(thanks[outcome] + "\n\nIf someone helped, a quick thank-you in the thread goes a long way.")


async def followup_job(ctx: ContextTypes.DEFAULT_TYPE):
    for ask in db.due_followups(FOLLOWUP_DAYS * 86400):
        db.mark_followup(ask["id"])
        c = db.response_counts(ask["id"])
        await safe_dm(ctx, ask["user_id"],
                      f"👋 Checking in on your ask:\n“{e(ask['need'][:140])}”\n\n"
                      f"{c['help']} member(s) offered to help. Did you get what you needed?",
                      outcome_buttons(ask["id"], include_not_yet=True))


# ---------------------------------------------------------------- find members
def member_card(m) -> str:
    return (f"<b>{e(m['name'])}</b> · {e(m['role'])}, {e(m['company'])}\n"
            f"Knows: {e(m['expertise'])}\nCan help with: {e(m['offers'])}\nContact: {member_mention(m)}")


async def find_begin(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    clear_flow(ctx)
    if ctx.args:
        await run_search(update, ctx, " ".join(ctx.args))
        return END
    await reply(update, "🤝 <b>Find members</b>\n\nWho are you looking for? Describe the experience.\n"
                        "<i>e.g. sold to hospitals · hired in Poland · Series A fintech investor</i>")
    return F_QUERY


async def find_query(update: Update, ctx):
    await run_search(update, ctx, update.message.text)
    return END


async def run_search(update: Update, ctx, query):
    results = db.search_members(query, exclude_user_id=update.effective_user.id)
    if not results:
        await reply(update, "No matching profiles yet. Try different words, or post an ask so the whole "
                            "community sees it.", M([[B("🙋 Ask the community", callback_data="menu:ask")]]))
        return
    await reply(update, f"Members matching “{e(query)}”:\n\n" + "\n\n".join(member_card(m) for m in results) +
                "\n\n<i>Only members who chose to be findable appear here. When you reach out, "
                "mention it's via the DVC community.</i>",
                M([[B("🔍 New search", callback_data="menu:find"),
                    B("🙋 Post an ask", callback_data="menu:ask")]]))


# ---------------------------------------------------------------- discussion suggestions
async def topic_begin(update: Update, ctx):
    if not await gate(update, ctx):
        return END
    clear_flow(ctx)
    await reply(update, "💬 <b>Suggest a discussion</b>\n\nWhat question or topic would you like the community "
                        "to discuss? The team picks suggestions to post.\n\n"
                        "<i>e.g. How are you budgeting for AI tooling in 2027?</i>")
    return T_TEXT


async def topic_text(update: Update, ctx):
    ctx.user_data["topic"] = update.message.text.strip()
    await update.message.reply_text("Should we post it with your name?",
                                    reply_markup=M([[B("Post with my name", callback_data="anon:0")],
                                                    [B("Post anonymously", callback_data="anon:1")]]))
    return T_ANON


async def topic_anon(update: Update, ctx):
    anonymous = update.callback_query.data == "anon:1"
    sub_id = db.create_submission("topic", update.effective_user.id, ctx.user_data["topic"], anonymous)
    await send_submission_card(ctx, db.get_submission(sub_id))
    clear_flow(ctx)
    await reply(update, "📨 Thanks! The team reviews suggestions and you'll get a note if yours is posted.", main_menu())
    return END


def topic_post_text(sub, m) -> str:
    by = "a member" if sub["anonymous"] or not m else member_mention(m)
    return f"💬 <b>Discussion</b>\n\n{e(sub['text'])}\n\n<i>Suggested by {by}. Share your view below 👇</i>"


# ---------------------------------------------------------------- admin approval
async def send_ask_card(ctx, ask):
    m = db.get_member(ask["user_id"])
    await notify_admins(ctx, f"🆕 <b>Ask #{ask['id']}</b> from {member_mention(m)}\n\n" + ask_post_text(ask, m),
                        M([[B("✅ Approve & post", callback_data=f"adm:a:{ask['id']}:ok"),
                            B("✖️ Reject", callback_data=f"adm:a:{ask['id']}:no")]]))


async def send_submission_card(ctx, sub):
    m = db.get_member(sub["user_id"])
    who = member_mention(m) if m else f"user {sub['user_id']}"
    if sub["kind"] == "intro":
        title, preview = "Intro", f"👋 <b>New member</b>\n\n{e(sub['text'])}"
    else:
        title = "Discussion suggestion" + (" (anonymous)" if sub["anonymous"] else "")
        preview = topic_post_text(sub, m)
    await notify_admins(ctx, f"🆕 <b>{title} #{sub['id']}</b> from {who}\n\n{preview}",
                        M([[B("✅ Approve & post", callback_data=f"adm:s:{sub['id']}:ok"),
                            B("✖️ Reject", callback_data=f"adm:s:{sub['id']}:no")]]))


async def admin_decide(update: Update, ctx):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return
    _, kind, item_id, decision = q.data.split(":")
    item_id = int(item_id)
    if not COMMUNITY_CHAT_ID and decision == "ok":
        await q.answer("Set COMMUNITY_CHAT_ID first.", show_alert=True)
        return
    who = e(q.from_user.first_name)

    if kind == "a":
        ask = db.get_ask(item_id)
        if not ask or ask["status"] != "pending":
            await q.answer("Already handled.", show_alert=True)
            return
        m = db.get_member(ask["user_id"])
        if decision == "ok":
            msg = await post_to_community(ctx, ask_post_text(ask, m), ask_buttons(ask["id"], {"help": 0, "same": 0}))
            db.approve_ask(ask["id"], msg.chat_id, msg.message_id)
            await dm_live_ask(ctx, ask, m, msg)
        else:
            db.reject_ask(ask["id"])
            await safe_dm(ctx, ask["user_id"], f"Your ask “{e(ask['need'][:80])}” wasn't posted this time. "
                                               "Someone from the team may reach out to help directly.")
    else:
        sub = db.get_submission(item_id)
        if not sub or sub["status"] != "pending":
            await q.answer("Already handled.", show_alert=True)
            return
        m = db.get_member(sub["user_id"])
        if decision == "ok":
            text = (f"👋 <b>New member</b>\n\n{e(sub['text'])}" if sub["kind"] == "intro" else topic_post_text(sub, m))
            if sub["kind"] == "intro" and m:
                text += f"\n\n— {member_mention(m)}"
            msg = await post_to_community(ctx, text)
            db.decide_submission(sub["id"], "posted", msg.chat_id, msg.message_id)
            if sub["kind"] == "intro":
                db.set_member_field(sub["user_id"], "intro_status", "posted")
                await safe_dm(ctx, sub["user_id"], f"🎉 Your intro is live{post_link(msg)}. Welcome aboard!", main_menu())
            else:
                await safe_dm(ctx, sub["user_id"], f"💬 Your discussion suggestion was posted{post_link(msg)}. Thank you!")
        else:
            db.decide_submission(sub["id"], "rejected")
            if sub["kind"] == "intro":
                db.set_member_field(sub["user_id"], "intro_status", "rejected")
                await safe_dm(ctx, sub["user_id"], "Thanks for your intro! The team will be in touch about it shortly.")

    await q.answer("Posted ✅" if decision == "ok" else "Rejected")
    stamp = f"\n\n{'✅ Approved' if decision == 'ok' else '✖️ Rejected'} by {who}"
    try:
        await q.edit_message_text(q.message.text_html + stamp)
    except BadRequest:
        pass


def post_link(msg) -> str:
    """Link to a message in a supergroup (works for members of private groups too)."""
    cid = str(msg.chat_id)
    if cid.startswith("-100"):
        return f' <a href="https://t.me/c/{cid[4:]}/{msg.message_id}">here</a>'
    return ""


async def dm_live_ask(ctx, ask, requester, msg):
    suggestions = db.search_members(f"{ask['need']} {ask['who']}", exclude_user_id=ask["user_id"], limit=3)
    text = f"🟢 Your ask is live{post_link(msg)}. You'll get a message whenever someone taps “I can help”."
    if suggestions:
        text += ("\n\nThese members' profiles match what you described. They've agreed to be suggested, "
                 "so feel free to reach out:\n\n" + "\n\n".join(member_card(s) for s in suggestions))
    text += f"\n\nI'll check in with you in {FOLLOWUP_DAYS:g} days."
    await safe_dm(ctx, ask["user_id"], text)


async def pending_cmd(update: Update, ctx):
    if not is_admin(update):
        return
    asks, subs = db.pending_asks(), db.pending_submissions()
    if not asks and not subs:
        await update.message.reply_text("Nothing waiting for review. ✨")
        return
    await update.message.reply_text(f"{len(asks)} ask(s) and {len(subs)} intro/discussion item(s) to review:")
    for a in asks:
        await send_ask_card(ctx, a)
    for s in subs:
        await send_submission_card(ctx, s)


async def stats_cmd(update: Update, ctx):
    if not is_admin(update):
        return
    s = db.stats()
    published = s["published_asks"] or 1
    closed = s["asks_closed"] or 1
    await update.message.reply_text(
        "📊 <b>Community measures</b>\n\n"
        f"<b>Members</b>\nProfiles: {s['members']} (findable: {s['searchable']})\nIntros posted: {s['intros_posted']}\n\n"
        f"<b>Asks</b>\nIn review: {s['asks_pending']} · Live: {s['asks_live']} · Closed: {s['asks_closed']} · "
        f"Not posted: {s['asks_rejected']}\n"
        f"Asks with ≥1 offer of help: {s['asks_with_offer']} ({100 * s['asks_with_offer'] // published}% of posted)\n"
        f"Help offers: {s['help_offers']} from {s['distinct_helpers']} members\n"
        f"“Same question” taps: {s['same_question']}\n"
        f"Closed outcomes: ✅ {s['outcome_helped']} helped · 🟡 {s['outcome_partial']} partly · "
        f"❌ {s['outcome_unresolved']} not helped "
        f"({100 * s['outcome_helped'] // closed}% fully helped)\n\n"
        f"<b>Discussions</b>\nSuggested: {s['topics_submitted']} · Posted: {s['topics_posted']}\n\n"
        f"<b>Repeat contributors</b> (2+ asks, offers or posted topics): {s['repeat_contributors']}")


async def chatid_cmd(update: Update, ctx):
    """Run /chatid in the community group to find COMMUNITY_CHAT_ID (and topic thread id)."""
    msg = update.effective_message
    thread = f"\nThread (topic) id: <code>{msg.message_thread_id}</code>" if msg.is_topic_message else ""
    await msg.reply_text(f"Chat id: <code>{update.effective_chat.id}</code>{thread}\n"
                         f"Your user id: <code>{update.effective_user.id}</code>")


# ---------------------------------------------------------------- wiring
async def cancel(update: Update, ctx):
    clear_flow(ctx)
    await reply(update, "Cancelled. What next?", main_menu())
    return END


async def on_error(update, ctx):
    log.exception("Error handling update", exc_info=ctx.error)


def build_app(token: str, request=None) -> Application:
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    builder = Application.builder().token(token).defaults(defaults).post_init(set_commands)
    if request is not None:  # used by tests
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private

    # One conversation covering every flow, so tapping a menu button mid-flow cleanly switches flows.
    entry = [
        CommandHandler("start", start, filters=private),
        CommandHandler("ask", ask_begin, filters=private),
        CommandHandler("find", find_begin, filters=private),
        CommandHandler("topic", topic_begin, filters=private),
        CommandHandler("profile", show_profile, filters=private),
        CommandHandler("myasks", my_asks, filters=private),
        CallbackQueryHandler(onb_begin, pattern="^menu:onboard$"),
        CallbackQueryHandler(ask_begin, pattern="^menu:ask$"),
        CallbackQueryHandler(find_begin, pattern="^menu:find$"),
        CallbackQueryHandler(topic_begin, pattern="^menu:topic$"),
        CallbackQueryHandler(show_profile, pattern="^menu:profile$"),
        CallbackQueryHandler(my_asks, pattern="^menu:myasks$"),
    ]
    conv = ConversationHandler(
        entry_points=entry,
        states={
            O_NAME: [MessageHandler(text, onb_name)],
            O_COMPANY: [MessageHandler(text, onb_company)],
            O_ROLE: [CallbackQueryHandler(onb_role, pattern="^role:")],
            O_EXPERTISE: [MessageHandler(text, onb_expertise)],
            O_OFFERS: [MessageHandler(text, onb_offers)],
            O_INTERESTS: [MessageHandler(text, onb_interests)],
            O_LOOKING: [MessageHandler(text, onb_looking), CommandHandler("skip", onb_looking)],
            O_SEARCHABLE: [CallbackQueryHandler(onb_searchable, pattern="^srch:")],
            O_INTRO: [CallbackQueryHandler(onb_intro_choice, pattern="^intro:")],
            O_INTRO_EDIT: [MessageHandler(text, onb_intro_edit)],
            A_NEED: [MessageHandler(text, ask_need)],
            A_WHO: [MessageHandler(text, ask_who)],
            A_DEADLINE: [CallbackQueryHandler(ask_deadline, pattern="^dl:")],
            A_CONFIRM: [CallbackQueryHandler(ask_confirm, pattern="^ask:")],
            F_QUERY: [MessageHandler(text, find_query)],
            T_TEXT: [MessageHandler(text, topic_text)],
            T_ANON: [CallbackQueryHandler(topic_anon, pattern="^anon:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=60 * 60,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CommandHandler("pending", pending_cmd, filters=private))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=private))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CallbackQueryHandler(toggle_search, pattern="^prof:toggle$"))
    app.add_handler(CallbackQueryHandler(ask_response_tap, pattern="^(help|same):"))
    app.add_handler(CallbackQueryHandler(close_prompt, pattern="^close:"))
    app.add_handler(CallbackQueryHandler(followup_answer, pattern="^fu:"))
    app.add_handler(CallbackQueryHandler(admin_decide, pattern="^adm:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(followup_job, interval=30 * 60, first=60)
    return app


async def set_commands(app: Application):
    await app.bot.set_my_commands([
        ("start", "Main menu"), ("ask", "Ask the community"), ("find", "Find members"),
        ("topic", "Suggest a discussion"), ("profile", "Your profile"), ("myasks", "Your asks"),
        ("help", "How this works"), ("cancel", "Stop the current step")])


def main():
    global db
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN in .env (get one from @BotFather).")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody can approve posts.")
    if not COMMUNITY_CHAT_ID:
        log.warning("COMMUNITY_CHAT_ID not set: run /chatid in your group, then set it.")
    db = DB(DB_PATH)
    app = build_app(token)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
