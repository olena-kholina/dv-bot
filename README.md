# DVC Community Bot

A Telegram bot for the DVC community built around four functions. Members use it in a private chat; anything that goes public is approved by you first.

| Function | What members do | What you get |
|---|---|---|
| 👋 **Member onboarding** | 7 quick questions (name, company, role, expertise, what they can offer, interests, what they're looking for), choose whether to be findable, then approve or rewrite a drafted intro. New group members get a "Set up my profile" button. | An intro to approve, and a searchable profile |
| 🙋 **Ask the community** | Describe the need → ideal helper → deadline, preview the post, submit. | An ask to approve. Once posted, members tap **I can help** or **Same question**. The requester is told privately who offered, gets up to 3 matching (opted-in) profiles, and after `FOLLOWUP_DAYS` is asked whether they got help. Closing marks the post ✅ Resolved. |
| 🤝 **Find relevant members** | `/find sold to hospitals` or the menu button. | Searches only members who opted in, ranking expertise and offers highest. |
| 💬 **Discussion suggestions** | Submit a question, choose named or anonymous. | A suggestion to approve and post. |

**Admin:** approval cards arrive in your DM with ✅ Approve / ✖️ Reject. `/pending` resends anything waiting; `/stats` shows the success measures: asks with at least one offer of help, outcomes (helped / partly / not helped), distinct helpers, intros posted and repeat contributors.

**Privacy:** only current members of the group can use the bot; only opted-in profiles appear in search or suggestions; members can hide themselves anytime in `/profile`.

## Setup (about 15 minutes)

1. **Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **Add it to your DVC group** and make it an admin (it needs to post messages, and to see members join for the welcome).
3. **Configure.**
   ```bash
   pip install -r requirements.txt
   cp .env.example .env     # paste BOT_TOKEN
   python bot.py
   ```
   Send `/chatid` in the group to get `COMMUNITY_CHAT_ID`, and `/chatid` in a private chat with the bot to get your user id for `ADMIN_IDS`. Put both in `.env` and restart.
4. **Try it.** DM the bot `/start`, go through onboarding, approve your own intro, post a test ask.

## Running it permanently

The bot runs as a single process with a local SQLite file (`dvc.db`), so any small always-on host works: a $5/month VPS (`systemd` or `tmux`), Railway, Render, or Fly.io. Keep `dvc.db` on persistent storage and back it up; that's where profiles and asks live.

Example systemd unit (`/etc/systemd/system/dvc-bot.service`):
```ini
[Unit]
Description=DVC community bot
After=network-online.target

[Service]
WorkingDirectory=/opt/dvc-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Notes and limits

- **Members must open the bot once** before it can message them (a Telegram rule). The group welcome button and the "I can help" flow push people there; anyone who taps "I can help" without having opened the bot still gets credited, and the requester is still notified.
- **Search is keyword-based** ("hospital" matches "hospitals", but "sold" won't match "selling"). For ~500 members that works well; a later upgrade could use an AI model to match on meaning.
- **Groups with Topics:** set `COMMUNITY_THREAD_ID` to post into a specific topic (e.g. "Asks & Intros").
- **Tests:** `python test_bot.py` runs every flow against a simulated Telegram API, without needing a token.

## Files

- `bot.py`: all conversation flows, approval, follow-ups, stats
- `db.py`: SQLite storage and member matching
- `test_bot.py`: end-to-end test
