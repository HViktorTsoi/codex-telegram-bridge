# One Bot, multiple local Codex conversations

`topics_bridge.py` reuses the single-thread bridge's native socket, attachment
handling and durable recovery. One Telegram poller routes the owner's private
topics to separate target queues and Codex subscriptions. Binding and repairing
existing conversations never create or fork a Codex conversation. A separate,
explicitly confirmed new-session wizard can create a new native conversation.

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
  "notification_mode": "completion",
  "preview_style": "status-bar",
  "status_card_pin": false,
  "host_id": "zbox",
  "root_target": "meeting",
  "initial_telegram_offset": 123456,
  "targets": [
    {"key": "meeting", "name": "会议记录", "thread_id": "EXISTING_MEETING_ID", "message_thread_id": 100},
    {"key": "instagram", "name": "Instagram 短片", "thread_id": "EXISTING_INSTAGRAM_ID", "message_thread_id": 101}
  ]
}
```

The original top-level `thread_id` must match `root_target`, which retains the
original session and main-chat system-command context. Ordinary topic-less All
messages are held until explicit target selection, which sends them immediately, rather
than automatically using that root or the most recent session.
`initial_telegram_offset` must come from the
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
conversation. **添加会话** first lists all working directories represented in
the native catalog, with a conversation count on each directory button. Select
a directory to show its sessions. Directory lists with more than 12 entries
have pages; **返回目录列表** returns to the directory list page containing the
selected directory. A session page shows one path at the top and only that
directory's conversation buttons below.
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
after a restart; this existing-session binding route never creates or forks a
Codex conversation.

Bound-session menus, the All target chooser, `/to` and `/rename` target menus
put the most recently used conversations first. The add-existing catalog orders
sessions by recorded use and directories by their most recently used session;
native updated-at order remains the stable fallback for sessions without a use
record. Catalog pages retain their saved ordering until explicitly reloaded.
The private `session_access_events` and `session_recent_access` tables record
accepted Telegram topic input and session commands, selected All deliveries,
delivered dispatches and completed renames under the original native ID.
Original event times and IDs prevent replays from changing the order. Bot API
does not report merely opening a topic; menu rendering, cancelled choices, bot
output and status updates do not count as access. Desktop-only use is not
recorded as Telegram access.

For a bound conversation, choose **修改名称** in its `/sessions` detail panel,
reply to the exact name prompt, and select **确认改名**. `/rename` in a bound
topic selects that conversation; `/rename <new name>` goes directly to review.
In All, either form first asks for a target without choosing a default. Rename
prompts and management panels appear in All. Only owner replies to the exact
ForceReply prompt supply a name; `/cancel` or the cancel button exits. Names
must be visible, single-line, at most 90 UTF-16 units and unique among other
bound conversations on this host.

Confirmation updates `thread/name/set`, verifies the same native ID, calls
`editForumTopic` on the existing topic ID, then saves local metadata. It never
starts a model turn or changes the IDs, history, active work, model, permissions,
explicit aliases or target key. The `ui_renames` and `ui_rename_prompts` ledgers
preserve exact bindings and progress through native, topic and local-save stages.
If a stage fails after a partial update, **继续同步名称** on the home panel or
the bound conversation's rename entry continues that operation. Repeated
confirmation does not create another conversation or topic. A changed binding
or a conflicting external rename stops synchronization for review; a missing
topic must be repaired before starting a new rename.

**新建会话** opens a local filesystem browser starting at the user's home.
Directory buttons, ten-entry pages, parent navigation and a dot-directory toggle
work without text entry. **输入路径** accepts absolute, home-relative and current
directory-relative paths; symlinks resolve to their physical directory.
**新建文件夹** accepts a single component, including Chinese and spaces, and
uses exclusive mkdir without replacing anything. **使用当前目录** prompts for a
name, then shows the host, full chosen directory and name for final confirmation.
Created folders remain if the user cancels.

Text entry uses owner-only ForceReply prompts tied to an exact chat, topic and
message. Only replies to these prompts enter the wizard, ahead of alias/command
dispatch; ordinary messages keep their normal route. Stale, cancelled and orphaned
wizard-prompt replies are rejected without reaching a model. Reply `/cancel` to
a prompt or use the cancel button. Prompt and editable-draft lifetimes are 15 minutes.

Final confirmation calls `thread/start` with only `cwd`, inheriting native machine
defaults for model, approvals and permissions. `thread/name/set` names the new
thread, then the ordinary exact-ID binding route creates its topic. Creation does
not call `turn/start`. See the [official app-server protocol](https://learn.chatgpt.com/docs/app-server).
The private `ui_new_drafts` ledger commits intent before starting, records the
returned ID before naming/binding, and prevents duplicate native creation. A known
ID can be continued from the home menu after naming/binding failure. A start call
interrupted without a known result becomes uncertain, including across restarts;
it must be checked in the existing catalog instead of automatically retried.
Topic-creation uncertainty retains the existing `binding_ops` protection.

`/status` shows the
native conversation's complete ID, model and effort. `/model`, `/effort` and
`/stop` affect that topic's conversation. Unknown topics are rejected. User-made
topics are not automatically bound or converted into new Codex conversations.

Global menus and dispatch summaries go to the main chat: `/sessions`, `/tasks`,
`/inbox`, `/to`, `/aliases`, `/help` and `/start`. Session commands (`/status`,
`/model`, `/effort`, `/format`, `/stop`) act on the original native conversation
and send their results, confirmations and errors back to the input topic, or
the main chat when entered there. Unknown commands and unbound-topic session
errors also reply at the input location without selecting another native target.
Old management menus migrate to a main-chat panel on their next use, keeping
the exact message/topic checks on callbacks and ForceReply prompts. Regular
output and activity indicators keep their original topics; already queued output
retains its stored destination.

All's ordinary text and media use a durable held-message selector. Each text
message is independent; an album is grouped after two seconds of quiet. The
selector shows the message preview. Choosing a local bound target immediately
imports the original message(s) into that exact native thread/topic queue, with
no second confirmation page. No attachment download or model request occurs
before selection. A late album member arriving before selection invalidates
the old selection buttons; members arriving after handoff require a fresh choice.
The original content and reply snapshot are retained before rewriting the
destination topic. Replies stay in the selected topic, without main-chat relay.
Find held content under `/sessions` → **待发送**. Cancelled, expired or stale UI
never falls back to the root session. Creator and dispatch ForceReply inputs,
plain-text explicit aliases and slash commands retain their existing paths.
Main-chat media captions cannot run slash or alias commands.

Selection also queues the original text or media caption in the selected
Telegram topic, prefixed with `📩 你在 All 说：` and followed by the saved readable
reply context. This plain-text copy uses a durable, deduplicated outbox key;
recovery can complete it after input import without importing the native input
again or starting another model turn. It copies text only, leaving attachment
delivery to Codex on the existing path. Unselected/cancelled drafts do not send
copies, and previously completed records are not backfilled.

The receiver validates guarded input's frozen native thread, target key and topic
before dispatch and again after attachment preparation. A changed/missing binding
fails closed. The selected import intent is persisted before handoff; original update
IDs and immutable binding metadata prevent repeat imports after a crash. This
does not add Telegram tabs or move the client's built-in All entry. Existing held
or review drafts are not sent automatically on upgrade. Clicking a valid legacy
target button only refreshes the selector; choose a target again in the new list
to send. Already displayed legacy **确认发送** buttons can still complete their
original handoff. Confirmations in
session binding, creation, renaming and explicit dispatch keep their existing flows.

Set `native_mirror: true` globally or per target to mirror new user inputs and public
agent replies from native client turns. It defaults to off and
establishes a fresh baseline on first enable, re-enable or topic repair, leaving
earlier messages untouched. Active turns baseline their existing visible items
and can continue mirroring newly completed content. It observes local native items only, starts no model
turns, and deduplicates Telegram-origin input/output. Reconnect recovery is
limited to the latest 30 native turns, matching the bridge's recovery window.
Images in native user inputs get a text placeholder. Explicit local image links
in public agent replies are snapshotted and uploaded through the normal outbox,
including mirrored desktop replies. PNG/JPEG/WebP/GIF are supported up to 20 MB;
large images, GIF and explicit photo-format rejections use document delivery.
Only final replies explicitly claiming to attach/send an image can use the last
`imageView` path from that same turn when no explicit image link is present.
Internal image inspection alone does not upload anything. Code examples and
other local files, reasoning, tool logs and internal agent messages are not
uploaded. Queued `media_json` includes an immutable private snapshot, content
digest and delivery state; saved topic IDs are retained through topic repair.
Upload intent is committed before the HTTP request. Uncertain sends and crashes
are not retried automatically; 429 retries reuse the saved bytes. Existing queued
text is not rewritten or backfilled during migration.

## Recommended presentation

New topic instances default to completion-only delivery with a persistent inline
status bar. For deployment and uniform migration, explicitly merge these fields
into the existing private `config.json`:

```json
{
  "notification_mode": "completion",
  "preview_style": "status-bar",
  "status_card_pin": false
}
```

This is a field fragment, not a replacement configuration. Preserve credentials,
owner, socket, native IDs and topic bindings. Target settings override global
settings; remove or align old target-level copies of these three fields when
choosing a uniform presentation. Explicit legacy settings remain supported and
are not silently overwritten. Updating source code or copying an example does
not edit private configuration or clear durable delivery records. Follow
[the deployment migration steps](DEPLOYMENT.md#呈现配置与旧实例迁移), restart the
correct instance with its actual Python interpreter, and verify each target's
effective `notification_mode`, `preview_style`, `preview_body_style` and
`status_card_pin` in `health.json`. Single-chat instances keep their existing
behavior and use the separate single-chat example.

After input commits to the durable inbox, a receipt event wakes a separate
preview loop. It attempts a received/preparing preview without waiting for native
readiness, attachment preparation, `turn/start`, a first reply or a history scan.
The receipt uses the real inbox identity and exact native/chat/topic route; it
does not invent a native turn or completion record. Disconnected and uncertain
inputs show waiting/checking labels. Additional input preserves active progress,
and the real turn adopts the receipt's display identity. The preview runs outside
native dispatch/reconciliation locks and shares a send lock with status updates,
so older responses cannot overwrite newer content. Direct topic input, selected
All input and imported dispatches follow this path; system commands, empty or
unauthorized input and unselected All drafts do not. Visible latency still
depends on Telegram and the network.

`status-bar` quietly creates one ordinary receipt message per task, then edits
that same message with cumulative public text and two inline status rows about
every 15 seconds. When the body is unchanged it edits only the keyboard; no
separate status bubble is sent. Status labels are disabled buttons. A verified
active native turn also gets an exact-turn **⏹ 停止当前任务** button; pending and
terminal displays do not. Controls validate owner, Bot, message, topic, binding
and turn before interrupting. Old buttons cannot stop a newer task, uncertain
interrupt requests are not repeated, and confirmations remain in the original
topic without model generation.

Different public source items use blank lines and `────────────` separators;
internal slices of one item join directly. A preview over 3800 UTF-16 units shows
the recent tail with an explicit notice, while complete text and immutable media
snapshots remain in the outbox. The bar belongs to an ordinary message and scrolls
with it; it is not a fixed screen overlay and has no draft expiry. Its initial
quiet creation may still produce an OS banner.

Only explicit native `completed`, `failed` or `interrupted` state plus a
successful full history scan releases a turn. An item with `final_answer` phase,
waiting for approval/input, disconnection or an unknown state is not proof of
completion. On success, public process items are combined into quiet packets and
the complete final assistant reply is delivered separately. Selection prefers
the last `final_answer`, with the last public assistant item as the fallback when
phase metadata is absent; a turn with no process sends only its final reply.
Process and final text are separately split at 3800 UTF-16 units with balanced
code fences and no source-text loss. Images and files remain separate quiet
uploads. Only the final reply's last text packet has a normal notification;
failed/interrupted output retains its combined body and corresponding status
explanation, with only its last packet notifying.

Successful text no longer appends “✅ 本轮任务已完成”. The final reply's last
packet retains the durable terminal keyboard. After an explicit delivery ACK,
the bridge cleans up that task's temporary body preview by its saved message ID.
Desktop mirror turns use the same policy. Held output does not block explicit
control replies such as `/sessions` or `/model`, and old completed history does
not produce new alerts.

The completion ledger freezes the native/chat/topic route and notification
intent. Repeated events and restarts do not create a second final alert; unknown
send outcomes are not automatically retried. Prepared or attempted legacy plans
keep their frozen content across upgrades, and sent history is not rewritten.
This changes presentation and timing without extra model calls, settings changes
or session replacement. Normal notifications still obey Telegram mute and system
Do Not Disturb. `disable_notification` means no sound, not a guarantee against
every OS banner; see [the notification parameter](https://core.telegram.org/bots/api#sendmessage).

### Optional preview body styles

`preview_body_style` defaults to `normal`. With `status-bar` or `status-draft`,
explicit `quote` wraps Rich public preview text in a non-collapsed blockquote;
explicit `footer` gives ordinary top-level paragraphs small theme-secondary text
without a quote frame. Headings, lists, existing quotes, code and tables keep
their normal structure. The client theme and text-size settings choose exact
colors and sizes. Validate a single target first if using Footer, then optionally
set it globally for targets without overrides. These styles change only the
intermediate preview, not formal process text, final replies, controls or saved
source. Plain output and formatting fallback preserve the original text; a code
upgrade does not automatically enable Footer.

### Explicit compatibility styles

Keep these only when deliberately selected; they are not the recommended values
for a new deployment:

| `preview_style` | Intermediate display |
|---|---|
| `plain` | One temporary plain draft with status and cumulative public text; animation is client-controlled |
| `block` | One Rich `thinking` draft block containing status and cumulative public text, never hidden reasoning |
| `status-card` | One ordinary status card edited in place, plus a separate cumulative body draft |
| `status-draft` | One ordinary cumulative body message edited per task, plus separate temporary status drafts |

Temporary drafts expire about 30 seconds after refresh stops and may need the
next refresh to reappear after switching apps. Unsupported `plain`/`block`
previews leave output buffered until completion rather than posting ordinary
progress messages. `status-card` and `status-draft` can create an initial quiet
ordinary message. These modes keep the 15-second cadence, frozen destinations,
full source content and completion-only notification rule. Explicit older styles
continue to take precedence; uniform migration should write `status-bar` and
check per-target overrides. See [Telegram's draft API](https://core.telegram.org/bots/api#sendmessagedraft)
and [client streaming behavior](https://core.telegram.org/api/bots/ai).

`status-draft` allocates a new status-draft ID after confirming newer ordinary
messages, attempting to place it below the newest content. Heartbeats and body
edits keep the current ID. Position and IDs are durable before requests, so
retries and restarts do not repeatedly rotate them; actual layout remains
client-controlled.

`status_card_pin` remains `false` by default. Only an explicitly selected
`status-card` setup should consider `true`; it quietly adds that status message
to the pinned list without clearing other pins. Bot API cannot choose whether
the client shows a pin only inside a specific topic, so this is not a fixed
topic-footer solution. See [the pin API](https://core.telegram.org/bots/api#pinchatmessage).

### Explicit immediate delivery and legacy activity cards

With `notification_mode: "all"`, status cards use Rich HTML bold text on the
first of two lines with `━━━━` separators on either side. Duration and update
time share the second line, retaining the 15-second update interval. With activity
indicators enabled, topic mode defaults to `activity_position: "bottom"`: newer
delivered replies, photos and ordinary topic inputs cause a replacement card at
the end of the conversation on the next eligible update (up to about 15 seconds).
Only after the new card's returned message ID is saved does the bot delete its old
status card. Without newer content, it edits the existing card. It does not post
a new card every 15 seconds. A completed card can follow its own turn's late final
reply or image; unrelated future messages do not resurrect it. Enabling the feature
does not repost historical completed cards. Status cards are ordinary messages,
so this follows the latest content rather than creating a fixed screen footer.

Set `activity_position: "inline"` globally or on one target to retain the original
in-place behavior. Relocation intent and old-card cleanup are durable. Uncertain
replacement sends keep the old card and are not repeated automatically; known
rate limits delay retry, and old-card deletion retries without sending another
replacement. Topic repair retains destination isolation.

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
