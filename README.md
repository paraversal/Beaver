# Beaver

A small Telegram support desk. Users DM the bot; each request becomes a ticket in
one private support group (optionally one of its forum topics). Admins answer with
an explicit command, so ordinary team chatter in the group never reaches a user.

The full design and operating rules are in [SPEC.md](SPEC.md).

## How it works

- A verified user sends a message, photo, or file in DM. The bot opens one ticket
  per user and posts one card into the support group (or configured forum topic).
  For text, the message is inside the card, so there is no duplicate reply.
  Photos/files are copied once as an attachment because a card cannot contain
  them.
- Admins work inside that chat. Plain replies are **internal** and are not sent
  to the user. Use `/reply <text>` as a reply to the card to deliver an answer;
  the user sees it prefixed with `👤 Admin:`, the text is folded into the card,
  and the admin's command message is deleted from the group.
- A user `/close` closes their ticket, updates the card with `🔒 User closed the issue.`, and sends the user the standard confirmation.
- `/close [text]` closes the ticket. The optional text and the close notice are
  sent as one `👤 Admin:` DM, separated by a line; the text is folded into the
  card as `🔒 Close message: 👤 Admin: ...`, and the command is deleted. Without
  text, the card gets `🔒 Closed by an admin.` and the opener gets a short
  “closed by an admin without a message” notice with the reopen instructions.
  React ✅ closes the same way.
- Further messages from the user are folded into the ticket card. The card gets
  a 🔴 **NEEDS REPLY** banner and one notification is posted (then edited as more
  messages arrive) rather than one ping per message. The notification is removed
  after a delivered `/reply` or `/close`; the card warning clears with it.
  Attachments are copied silently and noted on the card.
- `/reopen` reopens a closed ticket. `/open` lists the backlog and works in the
  topic or in an admin DM.
- A mistyped admin command (for example `/reply` without replying to a ticket)
  is answered by DM and deleted from the group. `/help` behaves the same way,
  so the group stays for tickets rather than bot chatter.
- A user message on a recently closed ticket reopens it and folds into the card;
  otherwise it starts a new ticket. `/new` forces a new one, and
  `/new <issue>` opens it immediately from the command text.

Users have `/start`, `/help`, `/status`, `/close`, and `/new`. The bot sets
separate command menus: user commands appear only in DMs, admin commands appear
only to administrators in the support group, and other group chats get no menu.
Admins can type `/help` in the support group; the guide is DM'd to them and the
command is removed from the group.

## Setup

Requirements: Python 3.14+, [`uv`](https://docs.astral.sh/uv/), and a Telegram
bot token from BotFather.

1. Create the support group. Either works:
   - a private **supergroup with Topics enabled**, and one topic such as
     `Tickets` that holds every ticket, or
   - a **plain group**, where every ticket goes into the chat itself.
2. Add the bot to the group and promote it to **administrator**. This is
   required; the bot refuses to start otherwise. Enable **Delete Messages** so
   the cleanup after `/reply`, `/close`, `/reopen`, and `/help` works; without it
   the bot warns at startup and leaves those commands visible.
3. In BotFather, disable privacy mode (recommended) so the bot can see every
   message in the group.
4. Install dependencies and create the env file:

   ```sh
   uv sync
   cp .env.example .env
   ```

5. Fill in `.env`:

   - `BOT_TOKEN` — from BotFather.
   - `SUPPORT_CHAT_ID` — the negative group ID. The old `SUPERGROUP_ID` key is
     still accepted as an alias; do not set both to different values.
   - `TICKETS_TOPIC_ID` — optional positive topic ID. Leave it out for a plain
     group (or to use a forum's General); set it only for a forum topic. Setting
     a topic on a group without Topics enabled fails at startup.
   - `SECRET_QUESTION` / `SECRETS` — the anti-spam question and accepted answers
     (comma-separated). Answers are normalized on load.
   - `ADMIN_IDS` — optional bootstrap/owner IDs. Normal group admins are picked
     up automatically.

6. Run the bot from the repository root:

   ```sh
   uv run main.py
   ```

   Startup validates the token, the group type, the topic (if configured), and
   bot-admin status.

   Without `--env-file`, `.env` is loaded if present; otherwise only real
   environment variables are used. For running on a server with Docker, see
   [DEPLOY.md](DEPLOY.md).

### Finding the topic ID

Only needed for a forum. The Bot API cannot list forum topics, so run the bot
without `TICKETS_TOPIC_ID`, send `/whereami` as a group admin inside the topic,
then put the reported `message_thread_id` into `.env` and restart.

## Staging

Use a **separate** test group (any group works, including a plain one without
topics) and a separate DB so tests cannot spam production:

```sh
cp .env.staging.example .env.staging
uv run main.py --env-file .env.staging
```

To start a test run from a clean ticket slate, add:

```sh
uv run main.py --env-file .env.staging --clear-staging-tickets
```

That deletes all tickets and ticket messages from the staging database and
resets ticket numbering, then starts polling normally. It is refused when
`APP_ENV=production`; verified users are kept, so you do not have to redo the
secret challenge.

Ideally use a separate test bot token. If you reuse the production token, stop
the production process first: Telegram's `getUpdates` queue has one consumer.
Never point `.env.staging` at the production group. Leave `TICKETS_TOPIC_ID`
unset there unless the test group is a forum.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers config validation, secret normalization, whole-chat and topic
URLs, DB ticket lifecycle and constraints, attention counters and notification
lifecycle, admin authorization caching, command parsing, inline `/new`, and
card/open-list formatting.

## Operating notes

- One bot process per token. Do not run a second poller.
- The command menus are scoped at startup: default commands are cleared, DMs get
  user commands, the support group gets admin-only commands, and all other
  groups get none. If the admin menu fails to set, check the log and the bot's
  administrator status; the bot still accepts typed commands.
- Back up the SQLite DB, for example:
  `sqlite3 data/beaver.db ".backup 'backups/beaver-$(date +%F).db'"`.
- Logs do not contain secrets or message bodies.
- A failed DM (user blocked the bot) marks the ticket unreachable and tells the
  admin; it does not retry forever.
- Do not commit `.env` files or the database.

## Commands

| Where | Command | Effect |
|-------|---------|--------|
| User DM | `/start` | Verify or show ticket status |
| User DM | `/help` | How support works |
| User DM | `/status` | Open ticket summary |
| User DM | `/close` | Close your open ticket; card updates and you get a confirmation |
| User DM | `/new [issue]` | Start a new ticket; inline text opens it immediately |
| Support chat | `/help` | Admin guide, sent by DM; the command is deleted |
| Support chat | `/reply <text>` | DM the user (marked `👤 Admin:`), fold it into the card, delete the command |
| Support chat | `/close [text]` | Close; optionally DM a final admin-marked message, fold it in, delete the command |
| Support chat | `/reopen` | Reopen a closed ticket; delete the command |
| Support chat / admin DM | `/open [page]` | Backlog, waiting-for-support first |
| Any admin chat | `/whereami` | Show chat/topic IDs and admin status |

Plain replies in the support chat are internal. They are never forwarded.
Follow-ups from the user update the card in place and raise a single
NEEDS REPLY notification; delivered `/reply` and `/close` text also appears in
the card, so the card is the ticket's single readable thread.
