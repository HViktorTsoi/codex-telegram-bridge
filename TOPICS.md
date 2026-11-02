# One Bot, multiple existing Codex conversations

`topics_bridge.py` reuses the single-thread bridge's native socket, attachment
handling and durable recovery. One Telegram poller routes the owner's private
topics to separate target queues and Codex subscriptions. It never creates or
forks a Codex conversation.

Enable Threaded Mode for the existing Bot in BotFather. Verify
`getMe.has_topics_enabled`, create private topics with `createForumTopic`, and
verify each existing native conversation using `thread/read` and `thread/resume`.
Also enable **Disallow users to create new threads** in BotFather while keeping
Threaded Mode enabled. Verify `getMe.allows_users_to_create_topics=false`.
Otherwise Telegram clients create a new topic before sending messages from the
main chat, even for `/tasks` and `/sessions`. This owner-only setting also disables
manual user topic editing/deletion; the bot can still create and repair topics.
See [Telegram's bot forum rules](https://core.telegram.org/api/forum#bot-forums).
Back up and stop the old collector before switching modes. Bind only explicit
existing IDs; an unrecognized or missing target is not a reason to create a new
conversation.

The normal configuration retains its credentials, owner, socket and `thread_id`.
Add these fields (the IDs below are placeholders):

```json
{
  "mode": "topics",
  "host_id": "zbox",
  "root_target": "meeting",
  "initial_telegram_offset": 123456,
  "targets": [
    {"key": "meeting", "name": "会议记录", "thread_id": "EXISTING_MEETING_ID", "message_thread_id": 100},
    {"key": "instagram", "name": "Instagram 短片", "thread_id": "EXISTING_INSTAGRAM_ID", "message_thread_id": 101}
  ]
}
```

The original top-level `thread_id` must match `root_target`. Messages without a
topic retain this original binding. `initial_telegram_offset` must come from the
old collector's database after a clean stop with an empty queue; it is used only
when creating `topics.sqlite` for the first time. Preserve the original database
and its backups. Do not change a target key to a different native thread: each
target database records its native ID and refuses such reuse.

Explicit replies append readable quoted material after two newlines within the
same native text input. Selected passages come first, followed by the parent
body for context; identical passages are not repeated. Rich bodies are flattened
to readable text. Message/topic IDs remain in private storage and are omitted
from the model prompt. Routing still follows the incoming topic, or an explicit
local alias dispatch. Quoted attachments are described,
never downloaded again. A topic's implicit creation-message reference adds no
context. Unquoted inputs and command/media classification remain unchanged.
The additive `inbox.reply_json` column is stored atomically with each input;
existing queued rows need no backfill and continue with their original input.

Use the usual `/usr/bin/python3 manage.py start|stop|status` on zbox. `manage.py`
selects the entry point from `config.json`; configurations without `mode: topics`
continue using `bridge.py`. Topic mode does not use `--notify`.

From the main chat or any private topic, `/sessions` opens the session menu in
the main chat. The home panel lists the working directory of each bound
conversation. **添加会话** groups the full native catalog by working directory,
showing one path at the top and only that directory's conversation buttons below.
Use **上个目录／下个目录** to switch directories. Groups with more than 12
conversations have internal pages; returning from a selection keeps the same
directory and page. A private SQLite catalog snapshot keeps navigation stable
across native updates and restarts. **重新加载** builds a new snapshot while
keeping the current directory when it still exists. Long paths retain both ends;
missing directory metadata has its own group. These are local metadata reads
and never start model turns. Select a conversation, check its name and full ID, and
choose **确认绑定到新话题**. The new topic becomes available without restarting
the collector. Bindings are written atomically to `config.json`, with previous
configurations in `binding-config-backups/`. The picker restores subscriptions
after a restart and never creates or forks a Codex conversation. Create any new
Codex conversation on the computer first, then refresh the picker.

`/status` shows the
native conversation's complete ID, model and effort. `/model`, `/effort` and
`/stop` affect that topic's conversation. Unknown topics are rejected. User-made
topics are not automatically bound or converted into new Codex conversations.

Management panels, command replies (including errors) and new dispatch summaries
are sent to the main chat. Session commands still act on the conversation bound
to the topic where they were entered. Old topic menus migrate to a new main-chat
panel on their next use, keeping the exact message/topic checks on callbacks and
ForceReply prompts. Regular conversation output and activity indicators keep
their original topics; already queued output retains its stored destination.

Set `native_mirror: true` globally or per target to mirror new user inputs and public
agent replies from native client turns. It defaults to off and
establishes a fresh baseline on first enable, re-enable or topic repair, leaving
earlier messages untouched. Active turns baseline their existing visible items
and can continue mirroring newly completed content. It observes local native items only, starts no model
turns, and deduplicates Telegram-origin input/output. Reconnect recovery is
limited to the latest 30 native turns, matching the bridge's recovery window.
Images get a text placeholder; local files, reasoning, tool logs and internal
agent messages are not uploaded. Status cards use Rich HTML bold text on the
first of two lines with `━━━━` separators on either side. Duration and update
time share the second line, retaining the 15-second edit interval and saved message ID.

Codex replies use Rich Markdown by default and contain only the body, without an
automatically added topic-title prefix. `/format rich` and `/format plain`
persist a preference for the selected conversation; `/format` reads it without
starting a model turn. Delivery keeps the original topic ID in both modes.
`rich_text` is an additive outbox column. Existing queued chunks remain plain,
and recovery reuses already-queued groups rather than recalculating boundaries.

`topics.sqlite` atomically records each accepted input's target and Telegram
offset. The per-target `targets/<key>/bridge.sqlite` retains dispatch state and
each reply's topic. A crash between handoff and acknowledgement is deduplicated.
Outputs and attachment albums retain their original destinations across
restarts. Failed/deleted topic delivery is never retried in another topic.
Older native conversations that do not implement `thread/items/list` are read
through paginated `thread/turns/list` with `itemsView: full`. Recovery preserves
native client IDs and item timestamps and never repeats an ambiguous request.

Only the existing allowlisted owner's private chat is accepted. There is one
poller per Bot; another Bot can continue using its independent state directory.
All targets in this first version use the same host/socket. Multi-host routing
would also require moving attachments onto their actual execution host.

Picker buttons belong to the owner's exact private chat, topic and message and
expire after 15 minutes. A repeated selection returns an existing binding.
Topic creation has a persistent operation record: when its network response is
uncertain, retrying does not blindly create another topic. If a topic ID was
recorded before interruption, startup finishes that binding using the recorded
ID. An uncertain operation requires reviewing the actual Telegram topic before
editing `binding_ops` to resolve it.

If a Telegram topic is deleted, open `/sessions`, select its already-bound
conversation, then **重新创建话题** → **确认重新创建话题**. This replaces the
Telegram destination while keeping the same native thread ID, target key and
queue. It does not delete or fork the Codex conversation. The conversation's
current native name is used for the replacement topic. A second click for the
old topic returns the replacement instead of creating another one.

The HTTP Bot API does not expose a dedicated topic-deletion service message.
Explicit `message thread not found` delivery failures mark that binding's topic
unavailable and permit rebinding. Closed topics, permission errors and network
failures do not establish deletion. An empty `editForumTopic` or typing request
can succeed even for a deleted topic and is not used as proof of existence.
The repair button remains available even before a delivery failure is observed.
Historical outbox rows keep their original topic; failed replies remain stored
and are not automatically resent, and native requests are never re-executed.

Validation: `python3 -m unittest test_session_picker test_topics_bridge test_bridge test_attachments test_bus_reminder`.
Deployment records distinguish simulated inbound probes from actual Telegram
uploads/downloads, native model turns and topic replies. A phone-sent message
in each topic completes the real inbound check.
