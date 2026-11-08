# Beaver — Telegram support bot

**Status:** proposed target design (not implemented yet)  
**Date:** 2026-11-08  
**Repo:** `Beaver`  
**Audience:** maintainers and support admins

> One-sentence goal: turn one topic of the support supergroup into a shared inbox
> where the only everyday actions are **reply** and **react ✅** — no assignments,
> no status commands, no per-admin configuration, and no ticket lost across
> restarts.

---

## 0. TL;DR

The current bot is a promising prototype: `/start` → secret question → ask for the
concern → post it to a supergroup. That is the whole working idea. The admin side
does not exist: there is no ticket ID, no stored mapping, no reply relay, no close
flow, and the process does not start as committed.

The target design is:

1. **One dedicated destination is the inbox.** A new ticket is one bot card
   (ticket number, user, time, status) with the first text message inside it.
   Media is copied as a reply to that card because a card cannot contain it.
   The destination is the support group itself, or exactly one forum topic when
   `TICKETS_TOPIC_ID` is set; every ticket message in a forum carries that
   topic's `message_thread_id`.
2. **An admin answers with an explicit `/reply <text>`.** The bot DMs the text,
   folds it into the card, and deletes the command from the group. No link, no
   copy-paste, no second chat to watch.
3. **Plain replies are internal by default.** The bot does not send them to the
   user. This makes side discussion safe without extra syntax.
4. **✅ reaction (or `/close`) closes a ticket.** `/reopen` reopens it; `/reply`
   to a closed ticket also reopens it automatically.
5. **`/open` is the only backlog view.** It lists tickets waiting on support
   first, with ticket number, age, user, excerpt, and a jump link.
6. **One open ticket per user.** Further user messages append to it; no duplicate
   tickets. A user can `/close` their own ticket.
7. **Everything durable lives in SQLite.** Verification, tickets, and message
   mappings survive restarts. No FSM memory, no Redis, no cron service.
8. **Admins are the supergroup's admins.** Promoting/demoting someone in
   Telegram is the only admin-list maintenance; no `.env` updates.
9. **The group and topic are configuration, not code.** A production profile and
   a staging profile (separate group, separate topic, separate database) run the
   same build; only the env file changes. Tests never touch the production
   supergroup.
10. **The bot is an administrator of the support group.** This is a deployment
    premise, confirmed for this project; the startup check enforces it.

The result: a support inbox with the ergonomics of a group chat, not a ticket
system.

---

## 1. What the bot does today

### 1.1 The happy path in the code

1. User sends `/start`.
2. Bot replies with a welcome and a secret question
   (`src/user_ticket_router.py:23-30`, text in `src/utils.py:7`).
3. User answers the secret. If correct, the bot says "That's right! What's your
   concern?" and moves to the concern state (`src/user_ticket_router.py:48-61`).
4. User sends their concern. The bot thanks them and posts a message to the
   configured supergroup (always General today; there is no topic support):
   `New ticket! by {full name}` + the raw concern text
   (`src/user_ticket_router.py:64-73`).
5. `/cancel` clears the current flow (`src/user_ticket_router.py:32-46`).

Everything else is a sketch: the admin handler prints a TODO
(`src/admin_router.py:13-19`), and the SQL layer is unused by the handlers.

### 1.2 Audit: what is actually broken

Severity is relative to "can this run a support desk today?", not to code style.

| # | Severity | Finding | Evidence |
|---|----------|---------|----------|
| 1 | **Blocker** | The process cannot start from the repo root: `src/app.py` imports `db`, but the package is `src.db`. Verified with `.venv/bin/python -c "import src.app"` → `ModuleNotFoundError: No module named 'db'`. | `src/app.py:4` |
| 2 | **Blocker** | There is no reply path. The supergroup handler is a `print` TODO; no ticket is stored, so there is nothing to map a reply to. | `src/admin_router.py:13-15` |
| 3 | **Blocker** | `App().ready()` returns `None`, so `main.py` passes `app=None` to polling. The dataclass field is `db_conn`, but `ready()` assigns `self.conn`. | `src/app.py:6-12`, `main.py:17,24` |
| 4 | High | The DB layer is dead code and its SQL is wrong. `new_ticket` would fail (`table Tickets has 7 columns but 6 values were supplied`); `get_all_tickets` would fail (`no such column: is_trusted`); `get_ticket_by_supergroup` has the typo `supergrup_chat_id` and filters on the wrong identifier. Both failures were reproduced against a fresh SQLite DB. | `src/queries.sql:1-14,19-29` |
| 5 | High | After a concern is forwarded, the FSM state is never cleared or changed. Every later message from that user is treated as another concern and creates another "ticket". | `src/user_ticket_router.py:64-73` |
| 6 | High | Admin routing is unreachable. The first handler matches **every** message in the supergroup, so the later `/open` handler is never reached; it also matches `/open@BotName`, which the exact `F.text == '/open'` does not. | `src/admin_router.py:13-19` |
| 7 | High | User-derived HTML is injected into HTML-parsed messages: `from_user.full_name` and `message.text` are not escaped. A name or message containing `<` can break the send (`can't parse entities`) or inject formatting/links into the "admin" side of the chat. | `src/user_ticket_router.py:73`, `src/utils.py:7` |
| 8 | High | Non-text messages are not handled. A photo reaches the concern handler as `message.text is None`, so the group receives the literal string `None`; captions and files are lost. | `src/user_ticket_router.py:69-73` |
| 9 | Medium | Verification is a dead end on failure: a wrong secret gets no reply and unlimited retries. `Ticket.trusted` and the `UntrustedContacts` table are never used. | `src/user_ticket_router.py:15-18,48-61`, `src/queries.sql:31-36` |
| 10 | Medium | Secret comparison normalizes only the attempt, not the configured secrets (`attempt.strip().lower() in env.SECRETS`). Uppercase or padded secrets in `.env` can never match. Attempts are logged in plaintext. | `src/utils.py:22-23`, `src/user_ticket_router.py:60` |
| 11 | Medium | All flow state is in the default in-memory FSM. A restart loses every in-progress ticket; a user who was mid-flow is silently ignored until `/start`. | `main.py:19-24` |
| 12 | Medium | User handlers are not restricted to private chats. A user's unfinished FSM state can follow them into other groups where the bot is present. | `src/user_ticket_router.py` (no `chat.type` guard) |
| 13 | Medium | No rate limits, dedupe, media limits, or delivery-failure handling. The bot says "Thanks! We will get back to you ASAP!" before the supergroup send, so a failed send is invisible to the user. | `src/user_ticket_router.py:69-73` |
| 14 | Medium | `src/db.py` loads `queries.sql` from the process CWD via a relative path; the connection is synchronous `sqlite3` and would block the event loop once handlers use it. Foreign keys are never enabled. | `src/db.py:3`, `src/queries.sql:27` |
| 15 | Low | No command menu, no `/help`, no error handler, no graceful shutdown, no tests, no setup docs. A working `test.db` sits in the tree and is not ignored. | `.gitignore:1-2`, `README.md`, no test files |
| 16 | Medium | No chat/topic routing. `SUPERGROUP_ID` is used directly and every send lacks `message_thread_id`, so tickets can only land in General. There is no way to point a test run at a different group/topic without editing code and the single `.env`. | `src/user_ticket_router.py:70-73`, `src/env.py` |

### 1.3 What that means in practice

- A user can create a "ticket" that admins can read but cannot answer through the
  bot.
- Two tickets from the same user are indistinguishable except by name and text.
- The first message after a successful ticket becomes a brand-new ticket.
- A photo ticket silently loses the photo and sends the text `None`.
- A restart loses the conversation.
- Every ticket lands in the same General chat; testing in the live group would
  spam it, and testing anywhere else currently means editing code.
- The bot cannot even launch from the repository root as committed.

The good news: the product idea is right, the code is small, and almost all of
the fixes are additive.

---

## 2. What it should be able to do

The target bot should:

- Verify a user once, then remember them.
- Accept text, captions, photos, video, documents, voice, stickers, and albums.
- Persist a ticket with a human-friendly number and a convenient reply target.
- Route every ticket to one configured destination: the whole support group, or
  one forum topic when configured; never scatter tickets across topics.
- Relay user messages to the ticket topic exactly once, with attribution.
- Deliver explicit `/reply` text to the user's DM exactly once.
- Treat plain ticket-topic messages as internal team chat; require an explicit
  command to deliver anything to a user.
- Let admins close, reopen, find, and discuss tickets without a dashboard.
- Keep the user informed about open/closed/unreachable tickets.
- Run against production or a separate staging group/topic from the same build,
  selected only by the env file and database path.
- Survive restarts and transient Telegram failures.
- Respect privacy and rate limits.
- Be boring to operate: one process, one SQLite file, no external services.

Explicit non-goals for v1: a web dashboard, AI auto-replies, email/Slack
integration, per-ticket forum topics, SLA teams, assignment/claiming, and
exposing admin identities to users. One dedicated destination (the chat itself
or one forum topic) is in scope and
required; a topic per ticket is not.

---

## 3. The core decision: explicit delivery in the ticket topic

### 3.1 Options considered

| Option | Admin effort | Works with no username? | Shared context/audit | Verdict |
|--------|--------------|-------------------------|----------------------|---------|
| **Reply in the ticket topic + explicit `/reply`** | One short command, no context switch | Yes | Yes, whole conversation is visible | **Chosen** |
| Reply in the topic with automatic delivery | One native "reply" gesture | Yes | Yes, but internal discussion is easy to leak | Rejected after review |
| Admin DMs the bot with a link/command (`/reply 42`) | Context switch; copy the ticket number or tap a link; then type | Yes | Partly; needs extra markers | Optional fallback (P2) |
| Admin DMs the original author directly | Only if the user has a public username; loses audit; exposes the admin | **No** | No | Rejected |
| Admin replies in a separate tool/web app | High | Yes | Yes | Rejected for this size |

The support chat (or its tickets topic) remains the admin home. But a plain reply
there is **team chat**,
not a message to the user. Delivery is explicit: `/reply <text>` sends, and
`/close [text]` can send the closing answer. This costs one short command and
removes the class of bug where an internal aside reaches a customer.

### 3.2 The rule that makes it safe

An admin message is user-facing only when it is an explicit command:

```
/reply <text>        → DM the text to the user, fold it into the card,
                       delete the command; reopen if closed
/close [text]        → close; optional text is DM'd and folded in first,
                       then the command is deleted
plain reply          → internal team chat; no DM
✅ reaction          → close without a message
```

`/reply` and `/close` must be sent as a reply to the ticket card or to a message
the bot relayed for that ticket, so there is no ticket number to type. A bare
`/reply`, the wrong reply target, or any other mistyped admin command is answered
by DM and deleted from the group; the bot never leaves an oopsi in the ticket
chat.

This is deliberately boring: the default is safe, and sending requires an
intent-revealing command. If an admin replies to a colleague, nothing is
delivered. If an admin wants to answer the user, they type `/reply`.

### 3.3 The everyday admin loop

1. Read the card in the support chat/topic.
2. Reply to the card (or a user relay) with `/reply <text>`.
3. The user gets the text in DM; the text is folded into the card; the command
   is deleted.
4. When it is resolved, reply with `/close [text]` (or react ✅ if no message is
   needed). The command is deleted too.
5. If the user needs something else later, use `/reply` again; a closed ticket
   reopens automatically.

That is the required vocabulary: `/reply`, `/close`, `/open`, and `/reopen`.
There is no `/note`; plain replies are already internal.

### 3.4 Why not DM links as the primary flow

A deep link (`t.me/Bot?start=reply_42`) is useful when an admin needs to reply
without the whole team seeing it, but as a primary flow it is worse: it is a
context switch, it needs signed tokens and an admin-reply mode, it hides the
conversation from other admins, and it does not work naturally with media and
edits. Keep it as an optional P2 feature; do not build it in v1.

### 3.5 One dedicated chat or topic, not one per ticket

A single thread has all the ergonomics of an inbox — reply threading, search,
`/open` links — without the sidebar graveyard. Topics are used as a **channel**,
not as a ticket container:

- **One destination** holds every ticket card and every relay for the support
  group. The destination is the whole support chat by default, or exactly one
  forum topic (for example `Tickets`) when `TICKETS_TOPIC_ID` is set.
- With a topic configured, every card, relay, notice, and card edit uses that
  `message_thread_id`; nothing goes to General. With no topic configured, every
  card is posted into the chat itself (a forum without a topic uses General, never
  whichever topic an admin happens to type in).
- The destination is uniform for all tickets: it is fixed at startup and stored
  on each ticket, so admin messages in other topics never redirect delivery.
- A topic ID may only be configured for a forum group; setting one on a plain
  group is a startup error rather than a silent fallback.
- The same build can point at a **staging group** via a separate env file
  (`.env.staging`). The staging group does not need Topics at all: a plain group
  is the simplest test bed.
- Do **not** create one topic per ticket: it multiplies cleanup, makes `/open`
  links inconsistent, and leaves dead topics behind.

> Testing note: a staging group is a real Telegram group. Use a **separate**
> group (plain is fine), a **separate** database, and ideally a separate test bot
> token. A staging run must never be able to post into the production chat.

---

## 4. Roles and vocabulary

| Term | Meaning |
|------|---------|
| **User** | A person in the bot's DM who needs help. |
| **Admin** | A current administrator of the configured support group (or an ID in the optional bootstrap `ADMIN_IDS`). |
| **Support group** | The private group configured by `SUPPORT_CHAT_ID`; a supergroup, optionally with Topics, or a plain group. |
| **Tickets topic** | The optional forum topic configured by `TICKETS_TOPIC_ID`. When set, it is the only place ticket traffic is posted; otherwise the whole group is. |
| **Ticket** | One user request, identified by `#<n>`, with a card and a conversation. |
| **Card** | The bot message that starts a ticket in the support group/topic. It is the canonical status anchor. |
| **User relay** | The first user message for media tickets, copied by the bot as a reply to the card. Text is rendered inside the card; later messages are folded into the card instead. |
| **Admin reply** | An explicit `/reply <text>` (or the optional text in `/close [text]`) that is delivered to the user. |
| **Internal message** | An admin message that is not delivered: a plain reply or any message without an explicit delivery command. |
| **Unreachable** | The user has blocked the bot, so an explicit `/reply` cannot be delivered. |

---

## 5. User experience

### 5.1 First contact and verification

Any private message from an unverified user starts verification; `/start` is the
explicit entry point and gets a greeting.

- If the user is already verified: greet, show ticket status if any, and treat
  the next non-command message as a ticket (or append to the open one).
- If not verified: show the challenge question. Keep the challenge answer in
  `.env`, never in the chat or logs.
- Wrong answer: "That's not it. Try again." Count the attempt.
- After `VERIFY_MAX_ATTEMPTS` in `VERIFY_COOLDOWN_MIN`, reply with the remaining
  cooldown and ignore attempts until it expires.
- Correct answer: mark the user verified **in the database**, reset the attempt
  state, and ask for the concern. The user does not have to answer a second
  `/start`.
- If the user sends several messages before verifying, do not send the question
  more than once per 30 seconds; process the latest message as an attempt.

Normalization for comparison: Unicode NFKC, casefold, strip, collapse internal
whitespace, drop trailing punctuation; compare against normalized configured
secrets. Do not echo the correct answer; do not log attempts.

> Persistent verification is a feature, not a security hole: returning users
> should not answer the question every time. The static secret is a speed bump
> against drive-by spam, not an authentication system. Rate limits are the real
> protection.

### 5.2 Opening a ticket

A **valid request** is a private-chat message from a verified user, under the
rate limits, with text or supported media, when the user has no open ticket.

The bot:

1. Creates the ticket row first (so a number exists).
2. Sends the **card** to the configured support chat (and to
   `TICKETS_TOPIC_ID` when one is set).
3. For text, renders the message inside the card and sends no second copy. For
   media, copies the attachment as a reply to the card (a card cannot contain it).
4. Stores the destination message IDs in the mapping (the copied media only).
5. Only after the topic delivery succeeds, DMs the user:
   `✅ Ticket #42 is open. Support will reply here. Reply any time to add
   details, /close when it's resolved.`

If delivery to the topic fails, the user is told that support could not be
reached and the ticket is not confirmed; no ghost ticket is left behind. For a
media ticket, if the card succeeds but the attachment copy fails, the card is
edited with a warning and the user is asked to resend; never silently lose the
content.

`/status` repeats the open ticket summary. Any message while a ticket is open is
appended, not turned into a new ticket.

### 5.3 Follow-ups and attachments

- Every user message for an open ticket is appended to the **ticket card**. The
  card is edited in place: no new support-chat relay and no new reply chain.
  The card shows `🔴 NEEDS REPLY · N new messages`, and one notification is
  posted as a reply to the card. Further messages edit that notification's count
  instead of pinging per sentence. The notification is deleted after a delivered
  `/reply` or `/close`; if deletion is denied, it is edited to `✅ #N handled`.
  The card warning clears at the same time. This keeps a chatty user from pinging
  the team for each sentence while still making an unanswered burst obvious.
- Text is appended (each addition capped at 1,000 characters for safety);
  photos, video, documents, voice, video notes, animations, stickers, contacts,
  and locations are copied silently into the same thread and noted on the card
  with an attachment marker.
  Telegram cannot append media to a text message, so the silent copy is the one
  exception to "no new message".
- Media groups (albums) may arrive as separate updates; each item is copied
  silently and adds a card marker. No item may be dropped.
- Unsupported message types (polls, invoices, games, etc.) get a short user
  message asking for text/file/link instead, and do not create a ticket.
- If the user repeats the exact same content within 60 seconds while the ticket
  is open, suppress the duplicate update (and do not create another ticket).
- If the user blocks the bot, they cannot send messages; if a `/reply` fails,
  the ticket is marked unreachable (see §6.6).

### 5.4 Closing and reopening

- `/close` from the user closes **their own** open ticket; the card in the
  support chat/topic is edited to closed and the user gets a short confirmation.
  The card update says `🔒 User closed the issue.` so admins can
  see who closed it. Any attention notification is deleted and the card warning
  clears; no extra admin notice is needed. The user receives a short confirmation
  with the reopen instructions. Admin closes are described below.
- A user message on a closed ticket reopens it if it is within
  `REOPEN_WINDOW_DAYS` (default 7). The card is edited to open/reopened, the
  message is folded into the card, and admins see the new state in the same
  thread.
- After the window, a message starts a new ticket; the card can include
  `↪️ follow-up to #42` for context.
- `/new` with no open ticket starts a new ticket and explicitly skips reopening
  the most recent closed one; `/new <issue>` opens it immediately from the
  command text, while bare `/new` arms the next message. With an open ticket it
  tells the user to `/close` first (no silent second ticket).
- The close notice tells the user: "Reply here to reopen, or /new for a new
  issue." This makes the `/new` distinction discoverable.

### 5.5 User and admin command menus

User commands are set for **all private chats**:

| Command | Effect |
|---------|--------|
| `/start` | Greet; verify if needed; show current status. |
| `/help` | How tickets work, what is shared with support, what not to send. |
| `/status` | Open ticket number, age, last update; or "no open ticket". |
| `/close` | Close the user's own open ticket. |
| `/new [issue]` | Start a new ticket if none is open; inline text opens immediately, otherwise the next message does. |

No other commands are exposed to users. `/cancel` is only needed for the optional
P2 reply-mode and can stay hidden or be omitted.

The bot clears the default command scope, sets the user menu for all private
chats, sets an **empty** menu for all group chats, and then sets the admin menu
for the **administrators of `SUPPORT_CHAT_ID`** only. This means user commands
never appear in the support group or in unrelated groups, and non-admin members
of the support group do not see admin commands. Telegram command scopes are
chat-level, so every topic in a forum sees the same admin menu; the handlers
still enforce the ticket topic.

Admin menu: `/help` (guide), `/reply`, `/close`, `/reopen`, `/open`,
`/whereami`. `/help` in the support group is an admin-only cheat sheet: explicit
`/reply`, `/close [text]`, plain replies are internal, `/open`, `/reopen`, ✅
close. It works in any topic of the support group; the guide is DM'd to the
admin and the command is deleted. The same DM-and-delete treatment applies to
mistyped admin commands so the group is not cluttered with bot chatter.

### 5.6 Example user copy

First contact:

> Hi Jane! To keep spam out, please answer this one question:
> **What's the name of the city our university is located in?**

Wrong answer:

> That's not it. Try again.

Correct / returning:

> Verified ✅ What do you need help with? Send a message, photo, or file.

Ticket opened:

> ✅ Ticket #42 is open. Support can reply here in this chat. Have more details?
> Just send another message here. Use /close when it's resolved.

Closed:

> Ticket #42 has been marked as resolved. If it is not actually fixed, reply here
> within 7 days to reopen it, or use /new for a different issue.

Unreachable is not shown to the user (they blocked the bot); it is an admin-side
state.

---

## 6. Admin experience

### 6.1 The card

A ticket starts with one bot message in the configured support chat (or its
configured topic):

```
🦦 #42 · open
Jane Doe (@jane) · 123456789
14:32 · 8 Nov 2026
```

- `#42` is the stable, human-friendly ticket number.
- The name/username/user ID identify the author. Usernames are shown when
  available so an admin can contact the user manually if the bot is blocked.
- The status line is edited on close/reopen/unreachable. The bot is the only
  editor of its own card.
- Every send and copy sets the delivery thread: `message_thread_id` =
  `TICKETS_TOPIC_ID` in a forum with a configured topic, General's thread in a
  forum without one, and nothing at all in a plain group.
- No per-card instructions: `/help` DM'd on demand and a pinned cheat sheet
  cover the workflow, so every card stays compact.
- The card is sent with link previews disabled.

For a text ticket, the first message is rendered inside the card (escaped and
capped at ~1,800 rendered characters), and no second message is sent. The card is
the canonical reply target. For media, the attachment is copied immediately after
the card as a reply to it because Telegram cannot embed it; that copy is mapped
too, so `/reply` or `/close` sent as a reply to either one resolves the ticket.

The card's **Updates** section is the ticket's readable thread. It holds user
follow-ups and delivered admin replies in order, including `/close [text]`. It
is not a full transcript (each entry is capped), but an admin can read the
conversation from the card without scrolling the group.

### 6.2 Sending a reply: `/reply`

A plain message in the support chat/topic is team chat and is not delivered. To send
text to the user, an admin replies to the card or a user relay with:

```
/reply Thanks — I've reset the invoice and it should appear in a minute.
```

Steps:

1. Resolve the replied-to message to a ticket via the card/root or mapping (§9).
2. Check that the author is a current admin (or bootstrap owner).
3. Require non-empty text; a bare `/reply`, no reply target, or a target that is
   not a ticket is DM'd back to the admin and deleted from the group.
4. If the ticket is closed, reopen it first (`reopened_count += 1`, card edited).
5. Send the text as a plain DM, prefixed with `👤 Admin:` so the user knows a
   human wrote it (no parse mode).
6. Append `👤 Admin: <text>` to the card's **Updates** section and edit the card.
7. Record the command as an `admin_reply` with the DM mirror ID for audit; the
   row is retained even though the command message is deleted.
8. Delete the command message from the group. If Telegram refuses (too old,
   missing delete permission), log it and leave the message; the delivery still
   stands.
9. On failure (user blocked the bot, account unavailable), do **not** pretend it
   was delivered: mark the ticket unreachable, reply in the support chat/topic with a
   short, actionable error, and do not retry in a loop. The command is not
   deleted, so the admin can see what happened.

The user sees a `👤 Admin:` label but not the individual admin's identity; the
bot is the sender. Admin identities are not exposed to users by default (see
§11.4).

`/reply` is text-only in v1. If an admin needs to send a file, they can send a
link or copy the file into the user's DM manually; media relay is P1. Plain text
is the common case and keeps the command unambiguous.

### 6.3 Internal discussion and mistakes

- A plain reply in the support chat/topic (no command) is internal team chat. The
  bot does nothing: no DM, no reaction, no mapping. This is the safe default.
- There is no `/note`. Plain replies already provide an internal channel; a
  separate note command added vocabulary without adding a capability. Historical
  `admin_note` rows may remain in old databases, but the command is gone.
- Commands `/close` and `/reopen` still work from a reply; they are not affected
  by the plain-reply rule.
- A mistyped admin command — bare `/reply`, `/reply` without replying to a
  ticket, `/close`/`/reopen` without a ticket, or a command in the wrong topic —
  is answered privately by DM and deleted from the group. If the DM cannot be
  sent (blocked bot, anonymous admin), the bot replies in the group instead and
  leaves the message so the feedback is visible.
- Successful `/reply`, `/close`, and `/reopen` commands are deleted after the
  action. `/help` is DM'd and deleted. `/open`/`/whereami` intentionally remain
  because their output is the point.

This is deliberate: the cost of a leaked internal message is higher than the cost
of typing `/reply`. The admin-only command menu and the `/help` guide make the
distinction visible; a pinned cheat sheet is still useful in a busy group.

### 6.4 Closing

Three equivalent ways:

1. React **✅** (or ☑️) to the card or any message mapped to the ticket.
2. Reply to the card with `/close [optional closing text]`.
3. The user uses `/close` from their DM.

On close:

- `status = closed`, `closed_at`, `closed_by_user_id` (internal audit).
- If `/close` included text, it is combined with the close notice into one
  `👤 Admin:` DM: the text, a separator, then the notice. The text is appended
  to the card under the close-message label.
- Card status line becomes `closed ✅` (with time); edit is best-effort.
- An admin close **without text** appends `🔒 Closed by an admin.` to the card
  and sends the opener: `👤 This issue has been closed by an admin without a
  message. Sorry! If this isn't resolved, reply here within 7 days to reopen it,
  or use /new for a different issue.`
- An admin close **with text** sends one `👤 Admin:` DM containing the text, a
  separator, and the resolved notice; the card shows the actual text under a
  `🔒 Close message: 👤 Admin: ...` label.
- A user `/close` sends the standard confirmation and marks the card with
  `🔒 User closed the issue.`
- The ticket disappears from `/open`.
- The `/close` command is deleted after the action. `/reopen` is deleted too.
- No topic chatter message is posted for a normal close; the card is the status.

`/reopen` (as a reply) or `/reply` to a closed ticket reopens it. A user message
within the reopen window reopens it too. On reopen, the card is edited, the user
is told (if the reopen came from `/reply`, the reply itself is the notice), and
the user's message is folded into the card rather than posted as a new reply.

A plain reply to a closed ticket does **not** reopen it; only `/reply` does. If
reactions are disabled in the support group, ✅ is unavailable; `/close` still
works. Startup checks fail if the bot is not an administrator; they warn if
reactions are disabled.

### 6.5 Backlog: `/open`

`/open` works in the support chat/topic and in an admin's DM. It lists open tickets,
split into:

```
Waiting for support (oldest first)
1. #42 · 3h · Jane (@jane) · “the invoice is wrong…” · <link>
2. #44 · 1h · Sam · [photo] · <link>

Waiting for user
3. #39 · 2d · Alex · “thanks, I'll test it” · <link>

Page 1/2 · use /open 2
```

- **Waiting for support** = last activity was a user message; oldest first. This
  is the work queue.
- **Waiting for user** = last activity was a delivered `/reply` or
  `/close [text]`; lower priority.
- The link jumps to the ticket card in the support chat/topic; use aiogram's
  `Message.get_url(include_thread_id=True)` (or `Chat.shifted_id`) so it works in
  private supergroups and forum topics.
- Page size ~10; `/open 2` for the next page. Do not list closed tickets.
- No inline buttons that mutate state from a list; links only, so a mistyped tap
  cannot close the wrong ticket.

### 6.6 Unreachable users

If sending a `/reply` or `/close [text]` message fails because the user blocked
the bot (`TelegramForbiddenError`) or the account is unavailable:

- Keep the ticket open; set `unreachable_at`.
- Edit the card to append `⚠️ user unreachable — bot blocked` and keep the
  username visible.
- Reply to the admin's command message with the exact problem:
  `⚠️ Could not DM @jane: the bot is blocked. The reply was not delivered.`
- Do not retry in a loop.
- If the user later unblocks the bot and sends a message, clear the flag and
  update the card; the admin can resend the answer.

Other Telegram delivery errors are reported the same way, with the specific
reason and an actionable suggestion (send a link instead, shorten the text, etc.).
`/reply` text over Telegram's 4096-character limit is rejected before sending, so
it is not mistaken for an unreachable user.

### 6.7 Admin authorization and `/whereami`

- Fetch the administrators of `SUPPORT_CHAT_ID`
  (`get_chat_administrators`) and cache them for ~5 minutes; the owner/creator
  is included.
- Optional `ADMIN_IDS` in `.env` is a bootstrap/owner allowlist that always
  works, even if the Telegram lookup fails.
- Accept anonymous group admins: `from_user` is `GroupAnonymousBot` and
  `sender_chat.id == SUPPORT_CHAT_ID`.
- Accept the group's linked channel when it posts as `sender_chat` and is the
  linked channel; for any other channel sender, verify or ignore.
- **Scope admin group handling to `SUPPORT_CHAT_ID`, and to `TICKETS_TOPIC_ID`
  when a topic is configured.** A message in another topic must not create,
  reply to, or close a ticket; in a plain group the whole chat is the inbox. The
  exceptions are `/whereami` (allowed anywhere in the support group and in DM for
  setup) and `/help` (allowed anywhere in the support group as the admin guide).
- Fail closed: if the admin list cannot be loaded and the sender is not in the
  bootstrap list, ignore the reply (never leak a user message to an unverified
  member).
- Re-check on every reply; a demoted admin loses access after at most the cache
  TTL. That is an acceptable trade-off; do not add a webhook for every promotion.
- Non-admins replying in the support chat/topic are ignored (optionally react 🔒).
  Never send them a message; do not reveal that a ticket exists.
- Non-admins in DM cannot use admin commands; reply politely or ignore.
- `/whereami` is the setup/debug helper, admin-only. It works in any topic of
  the support group and in a DM to the bot, and replies with: `APP_ENV`, the
  configured support chat ID, the current chat ID/type, `is_forum`, the current
  `message_thread_id` (or "General"), the configured `TICKETS_TOPIC_ID`, whether
  the bot is an administrator, and whether the sender is an administrator. It
  never creates a ticket. This is how a newly created staging topic's ID is
  discovered by running the bot without `TICKETS_TOPIC_ID`; once the ID is in
  the env file and the bot is restarted, forwarding into that topic begins. It
  also reports whether the group is a plain group, where no topic is needed.

### 6.8 What admins never have to do

| Task | How it is handled |
|------|-------------------|
| Add/remove an admin | Promote/demote them in Telegram; the bot picks it up. No config edit, no restart. |
| Assign a ticket | Not required; replies are visible and rare enough. No claim state to maintain. (Optional P2 if volume grows.) |
| Type a ticket number | Reply to the card/user message with `/reply`; no number to type. IDs are only for `/open` links and `/status`. |
| Copy the user's message | The bot relays it; the admin uses `/reply` when ready. |
| Mark "in progress" | Not required. The card and the support chat/topic show it; admins coordinate there. |
| Post "we're looking into it" | Use `/reply`; a plain reply stays internal. |
| Close by command | React ✅, or use `/close [text]`. `/close` works where reactions are hidden or disabled. |
| Check a dashboard | `/open` is the dashboard. |
| Update a status field | The card is edited by the bot on open/close/reopen/unreachable. |
| Notify the user | The bot sends `/reply` text and close/reopen notices. |
| Handle bot restarts | SQLite persistence; no re-verification, no lost thread mapping. |

---

## 7. Message and delivery formats

### 7.1 Ticket card

HTML, bot-authored. Everything user-derived is escaped with `html.quote`.

```
🦦 #42 · open
Jane Doe (@jane) · 123456789
14:32 · 8 Nov 2026
```

Status variants (edit the card on change):

```
🦦 #42 · closed ✅ · 15:10
🦦 #42 · open · reopened
🦦 #42 · open · ⚠️ user unreachable
```

Optionally append `↪️ follow-up to #38` when a new ticket continues an old one.

For text, put the first message in the card: one notification, one place to
reply, no duplicate. Escape it as HTML and cap it so the card stays under
Telegram's 4,096-character limit. For media, keep the card as the header and
copy the attachment separately, because a card cannot contain it. Disable link
previews on the card.

### 7.2 User relay and follow-up append

For text, the first user message is rendered inside the card and no second
message is sent. For media, the first attachment is copied into the support
chat/topic as a reply to the card; it does not need a header, because the card
identifies the user. Each later message for an open ticket is appended to the
card and the card is edited in place; it does not create another relay. The card
raises 🔴 NEEDS REPLY and one notification is posted, then edited to update the
count. Media is copied silently because Telegram cannot append it to a text
message; the card gets a marker.

If a copy is impossible (unsupported type, size), send a short system message in
the thread and tell the user what to do instead.

### 7.3 Admin reply

`/reply <text>` sends the text to the user's DM as plain text with a
`👤 Admin:` prefix. The DM is a single conversation per user and at most one
open ticket at a time, so there is no ambiguity about which ticket a reply
belongs to.

`/close [text]` with text sends one `👤 Admin:` DM containing the text, a
separator, then the standard close notice ("Ticket #42 has been marked as
resolved"). Without text it appends `🔒 Closed by an admin.` to the card and
sends the short "closed by an admin without a message" notice.
A plain ticket-topic message is not delivered at all; it is team chat.

`/reply` is text-only in v1. Links in the text work normally. Media relay is P1;
never send a partial message, and never silently fall back to a plain reply.

### 7.4 Notices

All bot notices in a ticket thread are mapped to the ticket, so `/reply` as a
reply to an error notice can resolve the ticket and answer the user (e.g., "try
again, they've unblocked the bot"). A plain reply to a notice remains internal.
Bot notices outside a ticket (e.g., the `/open` list) are not mapped.

### 7.5 Formatting and escaping rules

- Only bot-authored strings may contain HTML.
- Escape every user-controlled value (`full_name`, `username`, previews,
  excerpts) with `aiogram.html.quote`; do not interpolate raw text into a
  parse-mode message.
- For text, escape the user content and render it in the card; for media, prefer
  `copy_message` so the attachment is not parsed as bot HTML.
- Turn off link previews on cards and `/open` output; leave copied media alone.
- Normalize line endings and truncate previews at a character boundary
  (Telegram counts UTF-16 code units for entities and limits).

---

## 8. Ticket lifecycle

```
[unverified user] --correct secret--> [verified]
[verified] --private message, no open ticket--> OPEN #n
[OPEN] --user message--> OPEN (append)
[OPEN] --/reply--> OPEN (delivered) --user message--> OPEN ...
[OPEN] --plain reply--> OPEN (internal only, no delivery)
[OPEN] --✅ / /close [text] / user /close--> CLOSED
[CLOSED] --/reply / /reopen--> OPEN (auto-reopen, deliver)
[CLOSED] --user message within 7d--> OPEN (reopened, relay)
[CLOSED] --user message after 7d--> OPEN (new ticket, optionally ref old)
[OPEN] --14d user-silent after a /reply--> CLOSED (auto, if enabled)
```

Rules:

- At most one **open** ticket per user (partial unique index).
- A closed ticket is never deleted automatically in v1; retention is a separate
  P2 cleanup.
- Closing is idempotent; reopening is idempotent.
- Spam closes a ticket without a user notice and with `close_reason = 'spam'`
  (P2; optional `/spam` reply). Do not delete the ticket-topic thread
  automatically.
- `last_activity_at` and `last_activity_role` (`user` / `admin` / `system`) are
  updated on every relay; `/open` uses them to split the queue.
- Auto-close (if enabled) only closes tickets whose last activity was an admin
  reply and where the user has been silent for `AUTO_CLOSE_AFTER_DAYS`. Never
  auto-close a ticket where the user is waiting for support.

---

## 9. Data model

Use SQLite in WAL mode with foreign keys enabled. The schema is intentionally
small: users, tickets, and a message-mapping table. No content bodies are stored
except a short preview for `/open` (privacy and size); the support chat/topic is the
transcript.

### 9.1 DDL

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id              INTEGER PRIMARY KEY,          -- Telegram user id
    username             TEXT,
    full_name            TEXT NOT NULL,
    language_code        TEXT,
    verified_at          TEXT,                         -- NULL = unverified
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    cooldown_until       TEXT,
    force_new_until      TEXT,                         -- set by /new
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                   INTEGER NOT NULL REFERENCES users(user_id),
    status                    TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open', 'closed')),
    concern_preview           TEXT,                    -- first ~120 chars or [photo]
    origin_chat_id            INTEGER NOT NULL,
    origin_message_id         INTEGER NOT NULL,
    supergroup_chat_id        INTEGER NOT NULL,        -- SUPPORT_CHAT_ID at open time
    supergroup_topic_id       INTEGER NOT NULL DEFAULT 0, -- topic at open; 0 = whole chat
    supergroup_root_message_id INTEGER,                -- the card
    user_intro_message_id     INTEGER,                 -- the "ticket opened" DM
    opened_at                 TEXT NOT NULL,
    last_activity_at          TEXT NOT NULL,
    last_activity_role        TEXT NOT NULL DEFAULT 'user'
                              CHECK (last_activity_role IN ('user','admin','system')),
    closed_at                 TEXT,
    closed_by_user_id         INTEGER,
    close_reason              TEXT,
    reopened_count            INTEGER NOT NULL DEFAULT 0,
    unreachable_at            TEXT,
    last_delivery_error       TEXT,
    nudged_at                 TEXT,
    user_updates              TEXT NOT NULL DEFAULT '', -- appended follow-up text
    awaiting_admin_since      TEXT,                    -- first unanswered follow-up
    unanswered_updates        INTEGER NOT NULL DEFAULT 0,
    notification_message_id   INTEGER,                 -- one per unanswered burst
    UNIQUE (supergroup_chat_id, supergroup_root_message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
    ON tickets(user_id) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS tickets_open_order
    ON tickets(status, last_activity_at);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN (
                        'user_relay',      -- bot copy of a user message
                        'admin_reply',     -- /reply or /close text delivered to user
                        'admin_internal',  -- admin message not delivered
                        'system'           -- bot notice in the support chat/topic
                    )),
    sender_user_id  INTEGER,
    parent_message_id INTEGER,
    mirror_chat_id  INTEGER,
    mirror_message_id INTEGER,
    created_at      TEXT NOT NULL,
    edited_at       TEXT,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ticket_messages_by_ticket
    ON ticket_messages(ticket_id);

CREATE UNIQUE INDEX IF NOT EXISTS ticket_messages_by_mirror
    ON ticket_messages(mirror_chat_id, mirror_message_id)
    WHERE mirror_message_id IS NOT NULL;

PRAGMA user_version = 3;
```

Notes:

- `id INTEGER PRIMARY KEY AUTOINCREMENT` guarantees a ticket number is not reused
  after a delete; the current schema's `id INTEGER, PRIMARY KEY(id)` is a rowid
  alias and works, but `AUTOINCREMENT` makes the intent explicit.
- Timestamps are ISO-8601 UTC strings (`YYYY-MM-DDTHH:MM:SSZ`); display relative
  ages ("3h") or a configured timezone, not raw UTC.
- `supergroup_root_message_id` is nullable only during the brief create window;
  it must be set for a usable ticket.
- `user_updates` is the append-only text shown in the card's **Updates**
  section. Each addition is capped at 1,000 characters; the card displays only
  the latest ~1,600 rendered characters, preserving line breaks. Media
  follow-ups are copied silently and add an attachment marker, because Telegram
  cannot append media to a text message.
- `unanswered_updates` counts user follow-ups since the last delivered admin
  reply/close; `awaiting_admin_since` records the first one. Both clear on
  `/reply`, `/close`, or reopen. `notification_message_id` is the single
  support-chat notice for the burst; it is edited on further follow-ups and
  deleted (or marked handled) when the burst is answered. The card warning is
  derived from these fields, so it survives even if the notification is deleted.
- `supergroup_chat_id` + `supergroup_topic_id` are the **delivery coordinates**
  saved when the ticket opens. All later cards, relays, notices, and edits for
  that ticket use the stored pair, not the current env. The env only routes
  **new** tickets. A stored topic of `0` means the whole chat (no thread is
  sent). There is no silent fallback to another topic: if the stored
  destination is unusable, the send fails visibly and the user is not told the
  ticket opened.
- The partial unique index enforces one open ticket per user even under
  concurrent updates. Catch `IntegrityError` and treat it as "append", not an
  error.
- `ticket_messages` is the **reply-resolution index**. Any ticket-topic message
  the bot sends for the ticket (card, user relay, system notice) and any admin
  message (delivered or internal) gets a row. A reply is resolved by
  `(chat_id, reply_to_message.message_id)`; the `direction` and bot-vs-admin
  origin decide user-facing vs internal.
- `mirror_message_id` lets edits be mirrored to the DM copy and lets a future
  `/unsend` work.
- Do not store secret answers, full message bodies, or media bytes.

### 9.2 Useful queries (shape, not final code)

- `SELECT * FROM tickets WHERE user_id = ? AND status = 'open'`
- `SELECT * FROM ticket_messages WHERE chat_id = ? AND message_id = ?`
- `SELECT * FROM tickets WHERE status = 'open' ORDER BY last_activity_at`
- `UPDATE tickets SET status='closed', closed_at=?, ... WHERE id=? AND status='open'`
- `INSERT` + catch `IntegrityError` for the one-open-ticket index.
- Wrap ticket creation + first mapping in one transaction; send to Telegram
  outside/after the DB write, then update the message IDs. See §11.3 for the
  crash window.

### 9.3 Migrations

- Use `PRAGMA user_version` and a numbered list of migrations applied at startup.
- Back up the DB before applying a migration (or at least log the version and
  refuse to start on a future version).
- The current `create_tables()` with `BEGIN/COMMIT` inside a script works, but it
  is not a migration system; replace it with versioned migrations.
- The current `Supergroups` and `UntrustedContacts` tables are unused; drop them
  in the first migration or repurpose them. One configured support group and
  one destination is enough.

---

## 10. Message flow: end-to-end

### 10.1 User message → support chat/topic

```
User DM
  └─ aiogram user router (private chat only)
       ├─ unverified? → challenge / cooldown
       └─ verified?
            ├─ no open ticket → create ticket, send card with the configured
            │                    delivery thread (text embedded; media copied),
            │                    save mapping, DM "opened"
            └─ open ticket → dedupe/rate-limit, append text to the card and
                             edit it in place (media: silent copy + marker),
                             update last_activity
```

### 10.2 Support-chat admin command → user DM

```
Support-chat message (admin, in SUPPORT_CHAT_ID / TICKETS_TOPIC_ID when set)
  ├─ plain reply / media / chat → internal: no delivery, no mapping
  ├─ /reply <text> as a reply
  │    └─ authorize → resolve card/user message → non-empty text?
  │         ├─ closed ticket? reopen
  │         ├─ send plain text DM (👤 Admin:)
  │         ├─ append the text to the card's Updates, edit card
  │         ├─ map admin_reply + DM mirror id (audit)
  │         ├─ delete the command
  │         └─ on failure: mark unreachable + reply with the error (keep command)
  ├─ /close [text] as a reply
  │    └─ authorize → resolve → already closed? hint (DM/delete) :
  │         ├─ text? send plain DM first, append to card
  │         ├─ close + edit card
  │         ├─ map admin_reply (text) or system (no text)
  │         └─ delete the command
  └─ /reopen as a reply
       └─ authorize → resolve → reopen + edit card → delete the command

Mistyped command (no reply context, wrong target, wrong topic)
  └─ DM the admin a hint → delete the command
     (if the DM fails: reply in the group and keep the message)
```

A command without reply context is never guessed; the bot DMs the admin and
removes the oopsi. `/help` follows the same DM-and-delete path.

### 10.3 Reaction close

```
message_reaction update (admin, ✅, in SUPPORT_CHAT_ID)
  └─ resolve message → ticket
       ├─ already closed → no-op
       └─ close: update status, edit card, DM notice, clear from /open
```

`message_reaction` updates are **not** enabled by default in `getUpdates`; the
bot must explicitly pass `"message_reaction"` in `allowed_updates`, and it must be
an administrator in the chat. The update is not sent for reactions set by bots,
so the bot's own updates cannot recurse. If reactions are disabled in the support
group, `/close` is the fallback.

---

## 11. Reliability, security, and privacy

### 11.1 Startup checks

`APP_ENV` labels the deployment and selects the docs/setup profile; the same
checks apply to every profile. Fail fast with a clear logged error if:

- `BOT_TOKEN` is missing or invalid.
- `APP_ENV` is not one of `production`, `staging`, `development`.
- `SUPPORT_CHAT_ID` is missing/not an integer/not negative. If the legacy
  `SUPERGROUP_ID` is also set and differs, fail rather than guess.
- `get_chat(SUPPORT_CHAT_ID)` is not a supergroup or group.
- The bot is not a member or cannot send messages there.
- The bot is not an administrator. This is fatal, not a warning: reaction-close
  and full group visibility depend on it.
- `TICKETS_TOPIC_ID` is set but negative.
- `TICKETS_TOPIC_ID` is set on a group that does not have `is_forum=true`.
- The DB cannot be opened or migrated.

`TICKETS_TOPIC_ID` is **optional**. `0` or unset means the ticket destination is
the whole chat:

- Plain group (not a forum): cards go into the chat, no `message_thread_id` is
  sent, and every admin message in the chat belongs to the inbox. This is the
  expected staging setup.
- Forum without a topic: cards go to General (`message_thread_id=1`, i.e. no
  thread), because a forum always has a General topic. The bot never derives the
  destination from an admin message, so all tickets stay in one place.
- With a topic configured, every card, relay, and notice goes to exactly that
  topic, and admin messages in other topics are ignored.

`/whereami` stays available in every profile as the setup/debug helper; it
reports the current `message_thread_id` so a topic ID can be discovered before
it is configured.

The Bot API cannot validate an arbitrary topic ID without sending to it. Treat
the first send as the validation point: if it fails because the topic is closed,
deleted, or wrong, the error is logged and shown to the admin, and the user is
**not** told the ticket opened. Log the active profile and the target chat/topic
IDs at startup (IDs are not secret; tokens are).

Warn (not fatal) if the bot lacks the **Delete Messages** administrator right;
the delivered text still goes through, but command cleanup is best-effort and
the oopsi message may remain. Warn (not fatal) if reactions are disabled in the
support group; `/close` still
works.

Do not accept tickets if the configured inbox is unusable; tell the user support
is temporarily unavailable.

### 11.2 Delivery and failure handling

- Send the ticket card (with text embedded, or with media copied) before telling
  the user the ticket is open.
- If the destination send fails, do not confirm; tell the user to retry and log
  the failure.
- If a media copy fails after the card, edit the card with a warning, tell the
  user to resend, and keep the ticket open.
- If the DM copy fails, mark unreachable and tell the admin; do not mark it
  delivered.
- Respect `RetryAfter` and transient connection errors with a bounded retry and
  backoff. Do not blindly retry a send that may have succeeded; use the message
  mapping to avoid reprocessing the same update.
- A global error handler logs the update/ticket id and sends a short failure
  notice, throttled so an API outage does not flood the support chat/topic.
- Telegram retains updates for up to 24 hours; keep downtime short. Polling with
  the default pending-update behavior is fine for this size. Do not add a webhook
  until there is a real need.
- One process only. SQLite + polling is not designed for multiple bot instances;
  a second poller will conflict. Document this and let systemd restart a single
  instance.

### 11.3 Crash windows

The unavoidable sequence is: create ticket row → send card/content to the topic →
save topic message IDs. If the process dies between send and save, the topic has
a ticket the DB cannot map. Mitigations:

- Keep the DB write after the send as small and retryable as possible.
- On failure to save, attempt to delete the card/content (compensating action) and
  tell the user the ticket was not created.
- Optionally, a startup reconciliation can look for tickets with a null
  `supergroup_root_message_id` and mark them failed; do not blindly repost, or
  duplicates appear.
- Prefer a duplicate ticket over a silently lost user message, but make this
  window narrow and visible in logs.

### 11.4 Privacy and security

- **Admin identity:** hidden from users by default. The bot sends the reply; the
  DB records which admin sent it for audit. Do not add a public "admin name"
  header without a deliberate product decision.
- **User data:** store only IDs, names/usernames, ticket metadata, and a short
  preview. Message bodies live in Telegram, not the DB. If a transcript is needed
  later, add it deliberately with retention.
- **Secrets:** never log challenge attempts or answers. Normalize both sides.
  Store only verification status and attempt counters.
- **HTML injection:** escape all user/admin-controlled values in bot-authored
  HTML; for media, prefer `copy_message` so content isn't parsed as bot HTML.
- **Admin spoofing:** check group admin status server-side; never trust a
  forwarded message, a display name, or `author_signature`. Anonymous admins are
  allowed only via `sender_chat` in the configured support group.
- **Group access:** the support group must be private and admin-only. The bot is
  the only writer of cards/relays in the ticket destination; if a normal member
  can post in the support group, ignore anything that is not a reply to a ticket
  message. Only admins can use admin commands.
- **Staging isolation:** a staging profile must use its own group and its own DB,
  and ideally its own bot token. Never run production and staging with the same
  token. Log the target IDs at startup so a redirect is obvious in the first log
  line.
- **Rate limits:** protect admin attention, not just the API:
  - verification attempts: e.g. 3 per 10 minutes, 10/day;
  - new tickets: 1 per 30 seconds, 10/day per user;
  - messages: token bucket ~20/minute, ~200/day; drop with a single notice;
  - duplicate suppression: same content within 60s; same media file_id;
  - file size: reject/copy-fail gracefully above the Bot API limit (default
    50 MB; make it configurable).
- **DB file:** `chmod 600`, outside the repo by default (`data/beaver.db`), not
  committed. Backups are sensitive too.
- **Logs:** event ids and outcomes, not message bodies or secrets. Do not log the
  challenge answer (the current code does).
- **Retention (P2):** delete closed tickets and their mappings after
  `RETENTION_DAYS` (default 365). Telegram messages are not deleted by the bot;
  make that clear in the privacy note.

---

## 12. Configuration

Use the existing `environs` dependency (or `pydantic-settings`), validate at
startup, and fail with a readable message.

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `APP_ENV` | no | `production` | `production`, `staging`, or `development`; a label for logs and the setup profile. |
| `BOT_TOKEN` | yes | — | BotFather token; never log it. Use a separate test bot token for staging if possible. |
| `SUPPORT_CHAT_ID` | yes | — | Negative integer ID of the private support group (supergroup or plain group). The legacy `SUPERGROUP_ID` is accepted as an alias; if both are set and differ, fail. |
| `TICKETS_TOPIC_ID` | no | `0` (whole chat) | Positive thread ID of the one tickets topic, for a forum group only. `0`/unset: tickets go into the chat itself (a forum then uses General). Setting it on a group without Topics fails at startup. |
| `ADMIN_IDS` | no | empty | Bootstrap/owner IDs that always count as admins; comma-separated ints. |
| `SECRET_QUESTION` | yes | — | Human-readable challenge. |
| `SECRETS` | yes | — | Comma-separated accepted answers; normalized at load. |
| `DB_PATH` | no | `data/beaver.db` | SQLite file; directory created, mode 0600. |
| `TIMEZONE` | no | `UTC` | Only for displayed times; relative ages are preferred. |
| `VERIFY_MAX_ATTEMPTS` | no | `3` | Per cooldown window. |
| `VERIFY_COOLDOWN_MIN` | no | `10` | Cooldown after too many failures. |
| `REOPEN_WINDOW_DAYS` | no | `7` | User message after close reopens within this window. |
| `AUTO_CLOSE_AFTER_DAYS` | no | `14` | Only user-silent, admin-last tickets; `0` disables. |
| `SLA_NUDGE_HOURS` | no | `24` | One reminder if the user is waiting; `0` disables. |
| `RETENTION_DAYS` | no | `365` | P2 cleanup; `0` disables. |
| `MAX_TICKET_FILE_MB` | no | `50` | Informational pre-check; API limit may be lower. |
| `LOG_LEVEL` | no | `INFO` | Do not log message bodies. |

`SECRET_QUESTION`/`SECRETS` keep their current names and are normalized at load;
do not rename them for cosmetics.

### 12.1 Profiles: production and staging

The same code runs against either profile; only the env file and DB path change.
Nothing about the support group or topic is hard-coded.

| Profile | Env file | `APP_ENV` | Group/topic | DB |
|---------|----------|-----------|-------------|-----|
| Production | `.env` | `production` | Real support supergroup, optionally with one tickets topic | `data/beaver.db` |
| Staging | `.env.staging` | `staging` | Separate test group (a plain group is enough) | `data/beaver-staging.db` |

- Run staging with `uv run main.py --env-file .env.staging`; the flag defaults to
  `.env`. Do not rely on `source`/exported shell variables for profile selection.
- Start a clean test run with `--clear-staging-tickets`: it deletes all tickets
  and ticket messages from the staging DB, resets ticket numbering, keeps users
  and their verification, then starts polling. The flag is refused when
  `APP_ENV=production`.
- Use a different bot token for staging where possible. If you reuse the
  production token, **stop the production process first**: `getUpdates` is a
  single-consumer queue, and two pollers will steal updates from each other.
- A plain test group needs no topic at all: leave `TICKETS_TOPIC_ID` unset and
  tickets post straight into the group. If the test group is a forum and must use
  one topic, run `/whereami` inside it first, copy the reported
  `message_thread_id` into `.env.staging`, and restart.
- If Topics is enabled in the test group, `TICKETS_TOPIC_ID=0` posts to General.
- Existing tickets keep the `supergroup_chat_id`/`supergroup_topic_id` saved when
  they were opened. Changing the env does not retarget old tickets; only new
  tickets use the new profile.
- If both profiles run at once, use different tokens and distinct DB files, and
  check the startup log line that prints the active profile and target IDs.

---

## 13. Setup and operations

### 13.1 One-time setup (production)

1. Create the bot with BotFather; copy the token.
2. Create a **private supergroup** for support (a supergroup gives stable
   `t.me/c/...` links and clean admin semantics).
3. Recommended: enable **Topics** and create one topic named `Tickets`. This is
   the only place tickets are posted; keep General for human chatter. A plain
   group also works — then skip the topic and leave `TICKETS_TOPIC_ID` unset.
4. Add the bot and promote it to **administrator**. This is required, not
   optional. It needs to send messages in the topic; for ✅ close it receives
   reactions. Enable **Delete Messages** for command cleanup after `/reply`,
   `/close`, `/reopen`, and `/help`; without it, cleanup is best-effort and the
   startup log warns.
5. In BotFather, set privacy mode to **disabled** (recommended) so the bot sees
   the full topic, including admin-to-admin replies. The core design only needs
   replies to its own messages, but disabled is less surprising.
6. Get the group ID (negative, e.g. from an ID bot or `getUpdates`). For a forum
   topic, also get the topic ID: the Bot API has no "read topic ID" call, so
   either read the topic's `t.me/c/...` link, or start the bot without a topic and
   run `/whereami` inside it.
7. Put `APP_ENV=production` and `SUPPORT_CHAT_ID` in `.env` (plus the secret
   question/answers and optional settings). Add `TICKETS_TOPIC_ID` only for a
   forum topic. Keep the DB outside the repo or in ignored `data/`.
8. Run the bot from the repo root; verify the startup log line shows the profile
   and the expected support chat/topic IDs, and that the bot-admin check passes.
9. Send `/start` in DM as an admin, then `/open` to verify admin authorization.
   Send a test ticket from a second account and confirm it appears only in the
   tickets topic (or, without a topic, only in the support group).
10. Pin the admin cheat sheet (§14.3) in the support chat/topic once.

### 13.2 Staging setup

Use this to exercise the bot without touching production:

1. Create a **separate** group and add the bot as an administrator. A plain,
   basic group is enough and is the simplest test bed; other members are optional.
   Do not reuse the production group.
2. Copy `.env.staging.example` to `.env.staging` and set:
   - `APP_ENV=staging`
   - `SUPPORT_CHAT_ID=<test group id>`
   - leave `TICKETS_TOPIC_ID` unset (or `0`); only a forum test group needs it
   - `DB_PATH=data/beaver-staging.db`
   - ideally a separate test `BOT_TOKEN`; if reusing the production token, stop
     production first.
3. Run `uv run main.py --env-file .env.staging`. Tickets post straight into the
   test group. For a clean slate, add `--clear-staging-tickets` (deletes staging
   tickets/messages, keeps verified users; refused in production).
4. Verify the startup log shows the staging profile and the test chat.
   Run the manual checklist below. Confirm that **no** message appears in the
   production group or topic.
5. Never point `.env.staging` at the production chat/topic. Prefer a separate
   test bot so a mistaken run cannot post as the production bot.
6. Keep the staging DB separate. If you need to test migrations, copy a
   production backup, and never point staging at the production DB file.

### 13.3 Run

- From the repo root: `uv run main.py` (after fixing imports and dependencies).
  For staging: `uv run main.py --env-file .env.staging`.
- Use a systemd unit (or equivalent) with `Restart=on-failure`,
  `WorkingDirectory=<repo>`, and `EnvironmentFile=<repo>/.env`; one instance per
  token. Add a second unit with `EnvironmentFile=<repo>/.env.staging` only if it
  uses a different token.
- Logs to journald or a rotating file; no secrets, no message bodies. Include the
  active profile and target chat/topic IDs in the startup line.
- Do not run a second poller against the same token.

### 13.4 Backups

- Back up each profile's DB on its own schedule; production matters most:
  `sqlite3 data/beaver.db ".backup 'backups/beaver-$(date +%F).db'"`.
- Keep a few days; protect the files (they contain support metadata).
- Test a restore once, not just the backup command.

### 13.5 Upgrades

- Pull, run tests, restart. Migrations run at startup.
- For schema changes, back up first; the bot should refuse a future
  `user_version` rather than silently corrupting data.
- No blue/green needed; a short restart queues updates for up to 24 hours.
- Upgrade production and staging from the same build; apply config changes per
  env file.

### 13.6 Testing

Unit-test the pure pieces:

- secret normalization and attempt/cooldown logic;
- admin authorization (owner, admin, anonymous, channel, non-admin, stale cache);
- reply resolution by parent direction (bot vs admin, mapped vs unmapped);
- ticket state transitions (open/append/close/reopen/auto-close);
- one-open-per-user race handling;
- `/open` grouping/pagination and link construction;
- HTML escaping of names/previews;
- dedupe/rate-limit buckets;
- config parsing: `SUPPORT_CHAT_ID`/legacy alias conflict, `APP_ENV` values,
  whole-chat and topic ID parsing;
- destination routing: with a topic every card/relay/edit includes that
  `message_thread_id`; without one no thread is sent (a forum uses General);
  existing tickets use their stored `supergroup_chat_id`/`supergroup_topic_id`;
- `/whereami` content and admin-only access;
- newline-preserving card excerpts and the NEEDS REPLY banner;
- notification create/edit/delete lifecycle, attention counters, and v2→v3
  migration;
- `/new <issue>` parsing and immediate ticket creation.

Manual staging checklist:

1. Plain-group staging: `APP_ENV=staging`, `TICKETS_TOPIC_ID` unset; a new
   ticket's card and relay appear in the test group, and `/whereami` works in DM.
2. Forum staging with a topic ID set: cards appear only in that topic; General
   and other topics stay empty.
3. A topic ID set on a non-forum group refuses to start; a bot that is not an
   admin refuses to start.
4. Staging messages never appear in the production group; the startup log
   shows the staging profile and test chat.
5. New ticket: card + content; the user gets "opened" only after delivery
   succeeds.
6. Plain reply to the card or a user relay: no DM, no mapping, no reaction.
7. `/reply <text>` to the card, to a user relay, and to a second user relay; the
   user gets the text once; the text is folded into the card; the command is
   deleted; no 👌 is needed. A follow-up shows NEEDS REPLY and one notification;
   a second follow-up edits its count; `/reply` removes both.
8. `/reply` with no text or without reply context: admin gets a DM hint and the
   command is deleted. If the DM is blocked, the group gets the hint and the
   command stays. `/new <issue>` in a user DM opens immediately; bare `/new`
   waits for the next message.
9. `/help` in the support group: guide arrives by DM, command deleted.
10. ✅ close; `/close [text]` sends the text, folds it into the card, then the
    close notice; the command is deleted; `/open` excludes the ticket; user reply
    reopens; `/reply` to a closed ticket reopens; a plain reply does not.
11. `/open` ordering, ages, links (with thread ID), pagination, `/open@BotName`.
12. Commands outside the support chat/topic are ignored, except `/whereami`; `/open`
    works in admin DM. `/reply`/`/close`/`/reopen` in the support chat but wrong
    topic are DM'd and deleted.
13. Photo, caption, album, long text, code block, `<`/`&` in name/text.
14. Blocked user: `/reply` fails, card warning, no crash; an over-4096-character
    `/reply` is rejected before sending and does not mark the user unreachable.
15. Closed/deleted/wrong topic: first send fails clearly, no "ticket opened" DM,
    and a compensating delete is attempted.
16. Restart mid-flow: verification, open tickets, mappings persist; no duplicate
    ticket on redelivery.
17. Reactions disabled: `/close [text]` still works.
18. Bot removed/demoted at runtime: clear failure, no silent delivery.
19. No secret/message content in logs.

---

## 14. Implementation plan

### 14.1 P0 — make the core real

1. Fix startup: correct imports, `App` lifecycle, return `self`, DB path, async
   connection, graceful shutdown.
2. Add profile-aware configuration: `APP_ENV`, `SUPPORT_CHAT_ID` (with legacy
   `SUPERGROUP_ID` alias and conflict check), optional `TICKETS_TOPIC_ID`,
   `--env-file`, and startup checks. Add admin `/whereami` for topic-id
   discovery. Log the profile and target chat/topic at startup.
3. Rewrite `src/queries.sql` into the schema above + versioned migrations; add
   `aiosqlite`; enable WAL/foreign keys/busy timeout; store the ticket's
   chat/topic on open and use those coordinates for all later sends.
4. Replace the FSM flow with the stateless user flow:
   - private-chat guard;
   - persistent verification + cooldown;
   - one open ticket per user; append/dedupe;
   - ticket creation: card with text embedded (media copied) + `message_thread_id`
     + mapping + "opened" DM;
   - `/status`, `/close`, `/new`, `/help`.
5. Implement the admin path:
   - `SUPPORT_CHAT_ID` + `TICKETS_TOPIC_ID` filter; admin authorization;
   - plain replies are internal; no handler delivers them;
   - reply resolution by card/root or mapping for explicit commands;
   - `/reply <text>` delivery, card append, command delete / unreachable failure;
   - `/close [text]`, `/reopen`, `/open`; mistyped-command DM/delete;
   - ✅ reaction close (with `allowed_updates`);
   - status card edits in the ticket's topic.
6. Escape all bot-authored HTML; remove plaintext secret logging; add rate limits
   and duplicate suppression.
7. Add `allowed_updates`; clear default commands and set scoped menus (private
   user commands, empty group commands, admin-only support-chat commands); add
   `/help` admin guide, startup checks, and a global error handler.
8. Tests for the core; update `.gitignore` (including `.env*` with an
   `.env.example` exception) and README; add `.env.staging.example`; delete the
   stray `test.db` from the working tree.

### 14.2 P1 — polish and robustness

- Media/album handling and unsupported-type messages.
- `/reply` with media/captions, and explicit edit/delete semantics for delivered
  text (Telegram cannot retract a DM automatically).
- Auto-reopen on user message or `/reply`; unreachable handling.
- Dynamic admin cache and anonymous/channel senders.
- `/open` waiting-for-support vs waiting-for-user grouping, links, pagination.
- User `/close` and `/new` flow (`force_new_until`).
- Topic edge cases: closed/deleted topic, commands outside the topic, and moving
  a ticket only as an explicit admin action (not automatic).
- Better user/admin copy and a pinned cheat sheet, including the
  "plain reply = internal" rule.
- Dedupe/rate-limit edge cases; retry/backoff; throttled error alerts.
- Test matrix, staging runbook, and a profile-isolation check in CI where possible.

### 14.3 P2 — only if the team wants it

- Signed deep-link "Reply privately" flow from the card.
- Categories → optional forum topics (one per category, not per ticket).
- Auto-close inactivity task and one-time SLA nudge.
- Retention cleanup.
- `/ticket <id>` transcript view, `/stats`, `/spam`.
- Claim/assignment (only if volume makes duplicate replies a real problem).
- Backup automation inside the process (prefer an external timer).

### 14.4 Suggested module layout

Keep it small; do not build a framework.

```
main.py                 # --env-file, App lifecycle, dispatcher, allowed_updates, routers
src/
  config.py             # APP_ENV, profile selection, SUPPORT_CHAT_ID alias, topic/setup mode
  db.py                 # aiosqlite connection, migrations, repository helpers
  schema.sql            # migrations/DDL
  models.py             # Ticket/User/Message dataclasses or row helpers
  texts.py              # user/admin copy, escaped formatting
  auth.py               # admin cache + anonymous/channel rules
  tickets.py            # open/append/close/reopen, mapping, one-open rule, stored topic
  relay.py              # card, user relay, admin copy, notices, links, thread_id routing
  routers/
    user.py             # private-chat user flows
    admin_group.py      # tickets-topic commands, replies, reactions, /whereami
    admin_dm.py         # /open, /whereami, optional P2 reply mode
  maintenance.py        # P2 nudges/auto-close/retention
tests/
```

The existing `src/user_ticket_router.py`, `src/admin_router.py`, and `src/app.py`
can be replaced or evolved; the important thing is one owner for ticket state
(`tickets.py`) and one owner for message delivery (`relay.py`). The current
`src/db.py` loading a relative SQL path and the `App` dataclass mismatch should
not survive.

### 14.5 Rough effort

- P0: one focused implementation pass (roughly 1–2 days for an experienced
  aiogram developer, including tests and the staging profile).
- P1: roughly one more day.
- P2: optional, on demand.

The design deliberately avoids new infrastructure; most of the work is careful
state/mapping handling and edge cases.

---

## 15. Acceptance criteria

The spec is satisfied when all of these hold:

- [ ] From a clean checkout, the bot starts from the repo root with no import
      errors and validates its configuration. A topic ID set on a non-forum
      group, or a bot that is not an admin, refuses to start with a clear error.
- [ ] The bot runs in a plain group with no `TICKETS_TOPIC_ID`: tickets post into
      the chat itself, and admin commands work there.
- [ ] An admin can run `/whereami` in any topic of the support group and in DM;
      with no topic configured it reports the target as the whole chat.
- [ ] Command menus are scoped: user commands appear only in DMs, admin commands
      only to administrators in the support group, and unrelated groups show no
      bot commands. `/help` in the support group DM's the admin guide and deletes
      the command; typed commands still work even if the menu fails to set.
- [ ] A new user can verify, open a ticket, and see it appear **only** in the
      configured destination as one card with the number and first text; media
      gets one copied attachment. With a topic configured, General and other
      topics stay empty.
- [ ] A staging run via `.env.staging` + `data/beaver-staging.db` posts only to
      the test group; no staging message appears in production.
- [ ] An admin can reply to the card or the user's relay with `/reply <text>`;
      the user receives the text exactly once; it is folded into the card and the
      command is deleted.
- [ ] A plain reply to the card or a user relay sends nothing to the user; the
      bot does nothing (internal team chat). There is no `/note` command.
- [ ] `/close [text]` with text sends one `👤 Admin:` DM with the text, a
      separator, and the close notice; the text is folded into the card. Without
      text, the card says `🔒 Closed by an admin.` and the opener gets the short
      admin-closed notice. ✅ closes the same way. `/open` excludes it; `/reply`
      or `/reopen` reopens it.
- [ ] A `/reply` over Telegram's 4096-character limit is rejected before sending
      and does not mark the user unreachable.
- [ ] `/open` shows waiting-for-support tickets first, oldest first, with working
      links (including the thread ID) and pagination.
- [ ] A user's follow-up is folded into the ticket card (edited in place); it
      raises the NEEDS REPLY banner and one notification, which is edited for
      further messages, not duplicated. A second message does not create a
      second ticket; one open ticket per user is enforced.
- [ ] A delivered `/reply` or `/close` deletes the attention notification and
      clears the banner; a failed delivery leaves both.
- [ ] `/new <issue>` opens a ticket immediately with the issue text in the card
      and no duplicate relay; bare `/new` still arms the next message.
- [ ] A closed ticket is reopened by a user reply within the window, and a new
      ticket is created after it; `/new` can force a new one.
- [ ] Changing the env file does not retarget existing tickets: they keep using
      the `supergroup_chat_id`/`supergroup_topic_id` stored at open time.
- [ ] A blocked user cannot receive `/reply`; the admin sees a clear warning; the
      bot does not crash or loop.
- [ ] A restart preserves verification, open tickets, and reply mappings; no
      duplicate ticket is created for a redelivered update.
- [ ] Non-admin support-group members cannot send messages to users through the
      bot; plain replies stay internal; with a topic configured, admin commands
      outside it are ignored except `/whereami`.
- [ ] A name or message containing `<`, `&`, or long text cannot break the card
      or inject HTML.
- [ ] Reactions work only when the bot is admin and `allowed_updates` includes
      `message_reaction`; `/close [text]` works if reactions are disabled.
- [ ] A closed/deleted/wrong topic produces a clear failure, no "ticket opened"
      DM, and no silent fallback to another destination.
- [ ] No secret answers or full message bodies appear in logs.
- [ ] `README.md` explains setup, the admin gestures, profiles, and backups;
      `.gitignore` excludes `.env*` (with `.env.example`/`.env.staging.example`
      allowed), `*.db`, WAL/SHM files, `data/`, and `.venv/`.

---

## 16. Decisions and open questions

### 16.1 Decisions already made by this spec

| Decision | Rationale |
|----------|-----------|
| Reply in the support chat/topic + explicit `/reply` is the primary admin UX | Keeps the conversation in one place, but removes accidental internal-to-user leaks. |
| Plain ticket-topic messages are internal | The safe default; delivery requires an intent-revealing command. |
| `/reply <text>` and `/close [text]` deliver; plain replies are internal | Minimal vocabulary: answer, answer-and-close, or discuss. Delivered text folds into the card and the command is deleted. |
| `/reply` is text-only in v1 | Text is the common case; media relay adds copy/format edge cases that can wait. |
| One dedicated destination: the whole chat, or one forum topic | Keeps one canonical reply surface and avoids per-ticket topic clutter. A plain group needs no Topics setup at all; a forum can keep General clean. |
| `TICKETS_TOPIC_ID` is optional | Lets the bot run in any ordinary group, which makes staging trivial and keeps setup to a token plus a group ID. |
| Staging uses a separate group and DB via `.env.staging` | Tests exercise the real flow without spamming production; no hard-coded IDs, no code edits to switch targets. |
| No silent destination fallback | A topic ID on a non-forum group fails at startup; a forum without a topic always uses General, never whichever topic an admin typed in. Existing tickets keep their stored chat/topic coordinates. |
| One open ticket per user | Makes reply resolution unambiguous and stops duplicate threads. |
| Text in the card, media copied separately | One reply target for text; media still arrives intact. Text is escaped and capped so it fits Telegram's limit. |
| Persistent verification | Returning users should not re-answer; the secret is a speed bump, not auth. |
| Unique ticket numbers + `/open` links | Human-friendly references without a dashboard. |
| SQLite + polling + one process | Smallest operationally boring system that meets the requirements. |
| No per-ticket topics, no claim/assignment in v1 | Avoid sidebar clutter and state with no current need. |
| Delivered text is labelled `👤 Admin:` | Users can tell a human answer from an automated notice, without exposing which admin wrote it. |
| `--clear-staging-tickets` for clean test runs | Removes stale tickets that could affect routing, while keeping verified users. Refused in production. |
| User follow-ups append to the card, with one notification per unanswered burst | Avoids a ping per sentence while keeping an unanswered ticket obvious; the card stays the single ticket surface. |
| Admin identity hidden from users | Admins can reply candidly; audit is internal. |
| No DM deep link in v1 | The explicit `/reply` in the topic covers the common case; add a link only for sensitive cases later. |

### 16.2 Questions to confirm before implementation

The two structural questions are now settled: the bot will be an administrator,
and tickets live in one dedicated topic. Remaining defaults to confirm:

1. **Staging bot token:** use a separate test bot for staging, or reuse the
   production token (in which case production must be stopped during tests)? A
   separate token is safer and recommended.
2. **Topic name:** is `Tickets` the right name for the single ticket topic, or do
   you prefer something else? Only the configured ID matters to the code.
3. **Auto-close:** is 14 days of user silence a reasonable auto-close default, or
   should the first version never auto-close?
4. **Identity:** should `/reply` messages stay anonymous (recommended), or
   should a reply show the admin's first name?
5. **Retention:** is 365 days of ticket metadata acceptable, or should closed
   tickets be purged sooner?
6. **User close:** should users be able to `/close` their own ticket (recommended,
   less admin work), or only admins close?
7. **Deep link:** is a private-reply link actually needed, or is the ticket-topic
   reply sufficient?
8. **Secret:** keep the current question/answers, or rotate them? They are a
   speed bump; rate limits are the real gate.

If the answers are "separate staging token, `Tickets`, defaults", those are
adjustable without changing the design; the P0 implementation already follows
the explicit `/reply` and `/close [text]` flow above.

---

## Appendix A — Current code path, for reference

| File | Role today | Target role |
|------|------------|-------------|
| `main.py` | Creates `App`, bot, dispatcher, polling | Same, plus `--env-file`, lifecycle/allowed updates/error handler |
| `src/app.py` | Unused DB attempt; broken import/return | Replace with `db.py`/config lifecycle |
| `src/env.py` | Legacy env reader; removed | Replaced by `src/config.py` (`APP_ENV`, `SUPPORT_CHAT_ID` alias, topic/setup mode) |
| `src/db.py` | Loads `queries.sql` with sync sqlite3; unused | Async DB + migrations |
| `src/queries.sql` | Broken/unused schema and queries | Versioned schema + repository SQL |
| `src/user_ticket_router.py` | FSM flow; posts to group; no persistence | Stateless user flow with tickets/rate limits |
| `src/admin_router.py` | TODOs and an unreachable `/open` | Admin topic/DM handlers, explicit `/reply`/`/close` delivery, close/reopen |
| `src/utils.py` | Texts, name helper, logs, secret compare | Escaped texts, normalization, shared helpers |
| `.env` | Token, one admin ID, supergroup, question, answers | Production profile: token, `SUPPORT_CHAT_ID`, `TICKETS_TOPIC_ID`, DB path, question/answers |
| `.env.staging` | — | Staging profile: test group/topic, `data/beaver-staging.db`, ideally a test token |
| `test.db` | Empty tables, untracked working file | Move to `data/beaver.db`, ignored |
