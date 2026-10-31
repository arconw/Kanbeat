# Harness history and import markers

Developer reference for the harness-switching feature. It records what each harness exposes, what a later ticket may rely on, and what still needs a live check. It is not a user guide.

Inspected on 2026-10-31 against Codex CLI 0.166.0, OpenCode 1.19.3, Claude Code 2.1.316 and Pi 1.4.0. No model call was made, no harness session was started, and no conversation content was read into this document.

Source tags in square brackets refer to the list at the end.

## Summary

| | Codex | OpenCode | Claude Code | Pi |
|---|---|---|---|---|
| Structured history | app-server paged API | HTTP messages API | transcript `.jsonl` | session `.jsonl` |
| Stable event id | item `id`, turn `id` | message `id`, part `id` | record `uuid` | entry `id` |
| Kanbeat chooses the message id | yes, `clientUserMessageId` | yes, `messageID` | impossible | impossible |
| Marker survives in stored text | yes, from source | yes, from source | expected, unverified live | expected, unverified live |
| Silent delivery | `thread/inject_items`, not yet suitable for reconciliation | `noReply: true` | no documented API | no documented API |
| Delivery that starts a turn | `turn/start` | `POST /session/{id}/message` | `claude --resume … -p` | Pi RPC `prompt` or `pi -p` |

Marker approach: Codex and OpenCode work from source inspection; Claude and Pi are unverified and need a live check. Client-chosen message id: Codex and OpenCode work; Claude and Pi are impossible.

For every harness the safe delivery shape is the same: put the full conversation in a private context file and send a short message that tells the agent to read that file. The file only solves input size and command-line arguments; it does not extend the model's context window.

## Codex

The method named `thread/injectItems` in the ticket is `thread/inject_items` in the 0.166.0 schema; the Rust request type is `ThreadInjectItemsParams`. The rest of the starting facts hold.

### 1. Reading history and the end of a turn

Use the shared app-server host in `src/kanbeat/codex_host.py`. The v2 protocol exposes metadata through `thread/read`, turns through `thread/turns/list` and items through `thread/items/list`; `thread/read` with `includeTurns: true` is a deprecated full-history path for paginated threads, so new code should page instead. [C-SCHEMA, C-SRC]

- `thread/turns/list` takes `threadId`, optional `cursor`, `limit`, `sortDirection` and `itemsView`. The default direction is descending, the default `itemsView` is `summary`, and the page size is clamped to 1–100 with a default of 25. [C-SCHEMA, C-SRC]
- `thread/items/list` takes `threadId`, optional `turnId`, `cursor`, `limit` and `sortDirection`. The default direction is ascending, and the page size is clamped to 1–100 with a default of 25. An item-anchor cursor requires a `turnId`; an opaque string cursor does not. Each entry requires `turnId` and `item`; `startedAtMs` and `completedAtMs` are optional producer timestamps. The response also carries an optional `backwardsCursor` for reversing direction. [C-SCHEMA, C-SRC]
- A turn ends when its `Turn.status` becomes `completed`, `interrupted` or `failed`; `inProgress` is not finished. The `turn/completed` notification carries the same turn object. Do not infer the end of a turn from the last item. [C-SCHEMA, C-SRC]
- Conversation items are typed. `userMessage`, `agentMessage`, `reasoning`, `commandExecution`, `fileChange`, `mcpToolCall`, `dynamicToolCall`, `functionCallOutput`, `webSearch`, `imageView`, `imageGeneration`, `plan`, `hookPrompt`, `collabAgentToolCall`, `subAgentActivity`, `sleep`, `enteredReviewMode`, `exitedReviewMode` and `contextCompaction` are all visible in the item stream. [C-SCHEMA]
- The rollout files under `~/.codex/sessions` are a fallback only. Their records have a top-level `ordinal` that starts at 0 and is contiguous in observed files; `response_item`, `event_msg`, `turn_context`, `session_meta`, `compacted`, `token_usage_record` and `world_state` are among the record types. They are useful for diagnostics, not as the primary source. [C-FILES]

### 2. Identifiers and ordering

- `ThreadItem.id` is stable and unique within the thread; `ThreadItemEntry.turnId` places it in a turn. Turn ids are UUIDv7. [C-SCHEMA]
- `thread/items/list` returns stored order; use `sortDirection: "asc"` with the opaque `nextCursor` until `nextCursor` is null. Do not sort by `startedAtMs` or `completedAtMs`; they are optional producer timestamps. [C-SCHEMA, C-SRC]
- In rollout files the top-level `ordinal` is a second, file-local position. It is not the same value as an app-server item id. [C-FILES]

### 3. Choosing a message id and recognizing imports

- Kanbeat can choose the user message id. `turn/start` accepts `clientUserMessageId`; the resulting `UserMessageThreadItem` exposes it as `clientId`, and the raw rollout event item records it as `client_id`. [C-SCHEMA, C-SRC]
- The marker survives verbatim. The v2 `UserInput::Text.text` is copied into the core text input without rewriting it; `textElements` are separate spans and do not change the stored string. [C-SRC]
- Use both signals: the target message id is a fast path, the marker is the recovery path when the id was not recorded. An import should set `clientUserMessageId` and also put the marker in the text. [C-SCHEMA, C-SRC]
- `thread/inject_items` does not take a client id. It is documented as appending raw Responses API items without starting a user turn and the test suite confirms they are persisted and sent with the next model request, but the test reads them from the rollout, not from `thread/items/list`. Treat their visibility through the paged item API as unverified; do not use injection as the only import record. [C-SCHEMA, C-SRC, C-TEST]

### 4. Delivering a long context

- `turn/start` with `input: [{"type":"text","text":"…"}]` starts a turn. It rejects text longer than 1,048,576 characters (`1 << 20`) with `Input exceeds the maximum length of 1048576 characters.` The limit counts characters, not bytes, across the text inputs of the turn. [C-SRC]
- Deliver a short message that points at the context file. The model reads the file through its normal tools; the model's own context window still applies. A short prompt also avoids the argument-length limits of the interactive launcher. [C-SRC]
- `thread/inject_items` is the only silent append found. It accepts raw Responses API items, persists them and adds them to the next model request without starting a turn. It is not a substitute for a turn when the target must produce a reply, and its long-term visibility through the item list is not confirmed. [C-SCHEMA, C-SRC, C-TEST]

### 5. What cannot be exported

- Hidden and encrypted reasoning: `ReasoningThreadItem.encrypted_content` is not model-visible text, and raw `reasoning` response items can be encrypted. Export the summary or visible content only, or report reasoning as unavailable. [C-SCHEMA, C-SRC]
- Provider-internal records: `configuration_update`, `additional_tools`, `contextCompaction` and tool-search internals are protocol events, not conversation. [C-SCHEMA]
- Binary attachments: image and audio inputs are represented by file ids, URLs or paths (`LocalImage`, `Image`, `Audio`, `LocalAudio`, `imageView`, `imageGeneration`). Export the reference, not the bytes, and report the category. [C-SCHEMA]
- Hook prompts, sub-agent activity, review-mode markers, collab calls and sleep items are structural. They can be reported by type but should not be presented as user or assistant text. [C-SCHEMA]

### 6. Resume and session existence

- Resume an existing thread with `thread/resume` by `threadId`; use `excludeTurns: true` when the caller will page separately. `thread/read` returns metadata. [C-SCHEMA, C-SRC]
- A missing thread is reported as `thread not found: <uuid>` or `no rollout found for thread id <uuid>`. Kanbeat already treats exactly these messages as missing in `src/kanbeat/codex_host.py`. A disconnect, timeout or JSON-RPC transport error is temporary and must not trigger the fresh-session fallback. [C-SRC, K]
- Use `thread/items/list` or `thread/turns/list` to check that history is present; a successful metadata read alone does not prove that the history is complete. [C-SCHEMA]

The reader in `src/kanbeat/codex_history.py` pages this endpoint in ascending order and uses item ids as event ids. It returns an `available`, `missing` or `unreachable` outcome plus a completeness flag; a page-limit stop is available but incomplete. Import markers are always recognized; a user message `clientId` is treated as an import only when the caller lists it in `import_message_ids`, so ordinary client-chosen message ids stay original conversation. Reasoning, attachments, encrypted tool output and structural item types are reported as unsupported categories rather than mapped to user or assistant text. The reader only issues `thread/items/list`; it does not resume, inject or write anything. [K]

## OpenCode

### 1. Reading history and the end of a turn

Use the shared host in `src/kanbeat/opencode_host.py`; the server route root is `/session`. [O-SRC]

- `GET /session/{sessionID}/message` returns `SessionV1.WithParts[]` (`{info, parts}`) in chronological order. With no `limit`, the server pages internally in blocks of 50 and returns the whole session; with `limit`, it returns the newest `limit` messages in chronological order and exposes an older-page cursor through the `X-Next-Cursor` response header and a `Link` header. [O-SRC]
- `before` is an opaque cursor and is only valid together with `limit`. The cursor is a base64url JSON object containing the oldest message id and creation time of the returned page. A full read starts with the newest page, follows `X-Next-Cursor` while it is present and reassembles the pages oldest-first. An existing session with no messages returns an empty list, not 404; only a missing session returns 404. [O-SRC]
- Parts carry the conversation: `text`, `reasoning`, `file`, `tool`, `step-start`, `step-finish`, `snapshot`, `patch`, `agent`, `retry`, `compaction` and `subtask`. Tool parts contain `callID`, `tool`, and a state with `status`, `input`, and for completed calls `output`, `title`, `metadata` and attachments. [O-SRC]
- `time.completed` on an assistant message is not a run-completion signal. `SessionProcessor.cleanup` sets it at the end of every LLM step, and a message can get it from a completed subtask tool or shell command while the loop continues. The run exits only when the latest assistant message has `finish` set, `finish` is not `tool-calls` or `unknown`, it has no tool part that is neither provider-executed (`metadata.providerExecuted`) nor an orphaned interrupted tool, and its `parentID` is the latest user message id; a compaction stop or an error can also end it. The runner then goes idle, which removes the session from the status map. [O-SRC]
- A synchronous `POST /session/{id}/message` returns after that loop; `prompt_async` returns as soon as the prompt is accepted. For async delivery, confirm completion from the message state and from the session's absence in `GET /session/status`: `SessionStatus.list` omits idle sessions and `SessionStatus.get` returns `idle` for an absent id. Absence from the status map is not evidence that the session is missing; use `GET /session/{id}` for existence. Do not use `time.completed` alone. [O-SRC]
- Read in pages. Kanbeat's HTTP client reads at most 1 MiB per response and fails with `OpenCode response is too large` above that, so a full-history request without `limit` can fail on a large session. [K]

### 2. Identifiers and ordering

- Every message has an `id` beginning with `msg`; every part has an `id` beginning with `prt`. Both are stable. A message can hold several parts, so the message id identifies the message, not every normalized event. The reader uses the message id for the message's first event and stable part ids for additional text and tool events. [O-SRC, K]
- Message pages are ordered by `time_created` and then `id`; the page is reversed before it is returned, so the API result is chronological even though the database query is newest-first. Use the returned cursor for older pages rather than reimplementing the comparison. [O-SRC]
- Sessions are persisted in `opencode.db`; the `message` and `part` tables store the JSON payload with `id`, `session_id`, `time_created` and `time_updated`. Do not read the database directly when the HTTP API is available. [O-DB]

### 3. Choosing a message id and recognizing imports

- Kanbeat can choose the user message id. `POST /session/{sessionID}/message` accepts `messageID`; the schema only requires the string to start with `msg`, and the ID generator returns a supplied ID unchanged when it has the prefix. The endpoint stores that exact id. [O-SRC]
- The marker survives verbatim. A text part is persisted as a `TextPart` with the supplied `text`; the core only rewrites file, agent and subtask parts during resolution, not text parts. [O-SRC]
- Pass a unique marker in the text and use the returned user message `id` as the target-side id. A later export can recognize either. [O-SRC]

### 4. Delivering a long context

- `POST /session/{sessionID}/message` with `messageID`, `system`, and `parts: [{"type":"text","text":"…"}]` creates the user message. `noReply: true` returns immediately after the message is created: no model loop runs and no assistant turn starts. [O-SRC]
- The per-message `system` string is stored on the user message and is added to the system messages of the next model request; it applies while that user message is the latest one. Put the "context transferred from another harness" note there rather than in the conversation text. [O-SRC]
- Delivery can therefore be silent and exact. The route schema declares no body-size limit. A large body is still limited by the model context and by any limit the local server or intermediary imposes; that limit was not measured. Kanbeat's 64 KiB `MAX_PROMPT_BYTES` applies only to the command-line `--prompt` path, not to this HTTP path. [O-SRC, K]
- For a long conversation, the preferred shape is still a short message that points at a private context file. That keeps the request small and leaves the file readable until delivery is confirmed. [O-SRC]

### 5. What cannot be exported

- Reasoning parts are stored, but hidden reasoning and provider signatures must not be transferred; report reasoning as a category rather than copying it. [O-SRC]
- File parts and tool attachments are URLs, data URLs or base64 payloads. Export a reference or a filename, not binary content, and report attachments. [O-SRC]
- Text parts may set `synthetic` or `ignored`. Both are harness bookkeeping and must be skipped rather than exported as conversation. [O-SRC]
- `snapshot`, `patch`, `step-start`, `step-finish` and `retry` parts describe harness state or provider attempts, not conversation. `compaction` and `subtask` parts are context bookkeeping. Report them by type if they matter. [O-SRC]
- A plugin can transform messages through the `chat.message` hook; the core itself does not rewrite text parts. Do not rely on marker survival if a third-party plugin is installed. [O-SRC]

### 6. Resume and session existence

- `GET /session/{sessionID}` returns the session metadata; a missing session returns HTTP 404. Kanbeat's `opencode_session_exists` already distinguishes 404 from other HTTP and transport errors. [O-SRC, K]
- A connection error or timeout is temporary. The session status map (`GET /session/status`) is useful for a live check but is not an existence check. [O-SRC]
- An existing session is resumed by the interactive launcher with `opencode --session <id>`; the server keeps the session in its database. [K]

## Claude Code

### 1. Reading history and the end of a turn

- History is only available as transcript files under `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<munged cwd>/<session-id>.jsonl`. The directory name is the working directory with path separators replaced; Kanbeat locates transcripts by glob, so the exact munging rule is not required. [CL-FILES, K]
- Actual conversation records are `type: "user"` and `type: "assistant"` with `uuid`, `parentUuid`, `sessionId`, `isSidechain`, `timestamp` and `message`. `message.role` is `user` or `assistant`; `message.content` is a string or an array of `text`, `thinking`, `tool_use`, `tool_result` and `image` blocks. Tool results are user records whose content contains `tool_result` blocks. [CL-FILES]
- A turn ends at an assistant record with `message.stop_reason: "end_turn"`. A `system` record with `subtype: "turn_duration"` follows most turns and can be used as a second signal. Assistant records with `stop_reason: "tool_use"` continue the turn. [CL-FILES]
- File order is the append order. Do not sort by `timestamp`: in one observed transcript 1,210 timestamped records were out of timestamp order. Follow `uuid`/`parentUuid` for the logical thread and the file order for event order. [CL-FILES]
- Skip `isSidechain: true` records for the main conversation. Skip non-conversation record types (`attachment`, `api-request`, `api-request-blob`, `api-request-shape`, `file-history-snapshot`, `file-history-delta`, `queue-operation`, `last-prompt`, `mode`, `permission-mode`, `ai-title`, `pr-link`, `cost-state`, `tools-used`, `atis-latch`) and system metadata other than `turn_duration`. [CL-FILES]

### 2. Identifiers and ordering

- Each conversation record has a unique `uuid`. In an observed 4,094-record transcript there were no duplicate UUIDs. `parentUuid` links records into the conversation tree and `promptId` groups records of one prompt. [CL-FILES]
- Use the `uuid` as the event id and the file position as the position. `uuid` is stable in the file; `parentUuid` is useful for filtering sidechains and reconstructing the main path, but file order is the practical ordering guarantee. [CL-FILES]

### 3. Choosing a message id and recognizing imports

- Kanbeat cannot choose a message id. The CLI exposes `--session-id` for the session, not a message id, and the transcript assigns `uuid` itself. [CLI, CL-FILES]
- A marker in the user message is expected to survive because the transcript stores the submitted `message.content` text blocks. This was not verified with a live prompt, so treat it as unverified until a live check confirms the stored text is byte-for-byte identical. [CL-FILES]
- After delivery, find the marker in the transcript and record that user record's `uuid` as the target message id. Recognition later uses the marker first and the recorded `uuid` second. [CL-FILES]

### 4. Delivering a long context

- Resume and prompt in one non-interactive command: `claude --resume <session-id> -p "<short prompt>"`. This starts a model turn and writes a new user record. There is no documented silent append API. [CLI]
- A new native session uses `claude --session-id <uuid>`; `--fork-session` creates a new session id when resuming. Do not fork during an ordinary return to an existing harness. [CLI]
- Put the conversation in a private context file and send a short instruction to read it. The prompt is a command-line argument, so the operating system's argument limit applies; the model's context window applies separately. [CLI]
- Do not use the interactive transcript as a delivery channel. Write no transcript records directly; the CLI owns the file format and its locks. [CL-FILES]

### 5. What cannot be exported

- `thinking` blocks, including provider signatures, are hidden reasoning and must not be transferred. [CL-FILES]
- `image` blocks and image content inside `tool_result` blocks are base64 attachments. Export a reference or filename, not the payload, and report attachments. [CL-FILES]
- `attachment` records, `api-request*` records, file-history records, queue operations and system/hook metadata are harness bookkeeping, not conversation. [CL-FILES]

### 6. Resume and session existence

- `claude --resume <session-id>` resumes by id; `claude --continue` picks the most recent conversation in the current directory. [CLI]
- A missing session produces `No conversation found with session ID: <id>` (or `No conversation found to continue` for `--continue`). Kanbeat's `MISSING_SESSION` regex in `src/kanbeat/claude_runtime.py` already covers `no conversation found`, `session ... not found/does not exist` and `no such session`. [CL-BIN, K]
- The transcript file's existence is the cheapest existence check; `session_groups.py` already globs for it. A process failure with that missing-session text means the session is gone. A timeout, a launch failure with another message, or a filesystem I/O error is temporary. [K]

### 7. Corrections found while writing the reader

The reader in `src/kanbeat/claude_history.py` confirmed the record types and ordering above, and found these additions to the traversal rules:

- A user or assistant record can contain several content blocks and several tool calls or results. The record `uuid` identifies the record, not each block. The first event from a record uses the bare `uuid` as the event id; additional events from the same record use `uuid:<event-index>` so event keys stay unique. The file line index remains the position. [CL-FILES, K]
- Some user records are injected metadata (`isMeta: true`) and some are compaction summaries (`isCompactSummary: true`). Both are skipped: they are not original conversation. Compaction summaries are reported as an unsupported category. [CL-FILES]
- `tool_result.content` is a string or an array of `text`, `image` and `tool_reference` blocks. Only text is exported; images and other block types are reported. [CL-FILES]
- `custom-title` and `agent-name` records also appear in current transcripts and are non-conversation, like the record types already listed. Attachment records include hook output, tool input copies and file changes; they remain non-conversation and are reported as attachments. [CL-FILES]

## Pi

### 1. Reading history and the end of a turn

- Session files live under `<session dir>/--<path>--/<timestamp>_<session-id>.jsonl`, where `<session dir>` defaults to `~/.pi/agent/sessions` and can be overridden with `PI_CODING_AGENT_SESSION_DIR`, `--session-dir` or the `sessionDir` setting. The path component removes the leading separator and replaces `/`, `\` and `:` with `-`. [P-DOCS]
- The first line is a `session` header (`version`, `id`, `timestamp`, `cwd`). Later lines are entries with `type`, `id`, `parentId` and `timestamp`. The current format is version 3; older sessions are migrated when loaded. [P-DOCS]
- Conversation entries are `type: "message"` with `message.role` `system`, `user`, `assistant` or `toolResult`. Assistant messages contain `text`, `thinking` and `toolCall` content blocks; tool results contain `text` and optionally `image` blocks, plus `toolCallId`, `toolName` and `isError`. `custom_message`, `compaction`, `branch_summary`, `context_edit`, `model_change`, `thinking_level_change`, `usage`, `custom`, `label` and `session_info` are other entry types. [P-DOCS]
- A persisted assistant message has a terminal `stopReason` (`stop`, `length`, `error`, `aborted` or `deferred`); `toolUse` continues the response with tool calls, and `pending` is a streaming placeholder that is not persisted. That marks the end of a message or turn, not the end of all automatic work: `agent_end` closes one low-level agent run, but retries, overflow recovery, compaction, steering and follow-up work can still follow. The `agent_settled` event is the confirmation that Pi has no remaining automatic work; a successful `prompt` response only confirms acceptance or queueing. [P-DOCS]
- A file-only reader can use the terminal `stopReason` to cut turns. A delivery confirmation must wait for `agent_settled` in RPC mode or for the process to exit in one-shot `-p` mode, not for `agent_end`. [P-DOCS]
- The active branch is the root-to-leaf path. The RPC API exposes the leaf explicitly: `get_entries` returns append-order entries plus `leafId`, `get_messages` returns the active context. A file-only reader must follow `parentId` from the last appended entry; Pi does not write a separate leaf marker, so a session where the user navigated back without sending a message may not have the last entry on the active branch. [P-DOCS]

### 2. Identifiers and ordering

- Every entry has a stable `id` (usually an 8-character hex string, sometimes a full UUID) and a `parentId`. `get_entries` uses an entry id as a durable cursor: pass the last seen id as `since` to get strictly later entries. [P-DOCS]
- File append order is the order of entries. `parentId` gives the tree; for the common case of a stopped session, the last entry is the leaf and the root-to-leaf chain is the active conversation. [P-DOCS]
- Entry timestamps are ISO 8601; nested message timestamps are Unix milliseconds. Use them for display, not for ordering. [P-DOCS]

### 3. Choosing a message id and recognizing imports

- Kanbeat cannot choose a message id. The RPC `prompt` command has no message-id field; Pi assigns the entry id when it appends the user message. [P-DOCS]
- A marker in the user message is expected to survive because the user content is stored as a `UserMessage.content` string. This was not verified with a live prompt, so treat it as unverified until a live check confirms the stored content is unchanged. [P-DOCS]
- After delivery, find the marker in the session entries and record that entry's `id` as the target message id. Avoid a leading `/` in the marker or prompt: Pi expands skill commands and prompt templates before sending. [P-DOCS]

### 4. Delivering a long context

- RPC mode (`pi --mode rpc --session-id <id>`) accepts a `prompt` command. The response says whether the prompt was started, queued or handled, and events continue asynchronously; this starts a run, not a silent append. [P-DOCS]
- For a one-shot delivery, `pi -p --session-id <id> "<short prompt>"` runs once and exits. `--session-id` opens the exact project session or creates it if absent. `--session` opens by path, exact ID or partial ID and may offer a cross-project fork, so use `--session-id` for deterministic delivery. [P-DOCS]
- Put the conversation in a private context file and send a short instruction to read it. Do not pass the file as an `@file` argument when the goal is a short prompt: `@path` includes the file in the first prompt. The model's context window still applies. [P-DOCS]
- There is no silent delivery path in the documented RPC commands. `bash` output is stored but only reaches the model on the next prompt, and `custom_message` entries are written by extensions, not by a supported delivery command. [P-DOCS]

### 5. What cannot be exported

- `thinking` blocks, especially redacted blocks whose payload is only in `thinkingSignature`, are hidden reasoning and must not be transferred. [P-DOCS]
- `image` blocks are base64 data. Export a reference or filename, not the payload, and report attachments. [P-DOCS]
- `compaction`, `branch_summary` and `context_edit` entries change future context but are not conversation messages. `custom_message` entries are extension-injected context and are not original conversation. `custom`, `label`, `usage`, `model_change`, `thinking_level_change` and `session_info` are bookkeeping. Report them by type if needed. [P-DOCS]
- `bashExecution` is a separate message role for direct shell commands, not an LLM tool result. Label it if it appears in an export. [P-DOCS]

### 6. Resume and session existence

- `pi --session-id <id>` opens the exact project session or creates it if absent; `pi --session <id>` opens an existing session by path, exact ID or partial ID. Use the file check first when a missing session must be distinguished from a new one. [P-DOCS]
- The session file's existence is the primary check: `~/.pi/agent/sessions/--<path>--/*_<id>.jsonl` (or under `PI_CODING_AGENT_SESSION_DIR`). Kanbeat's `pi_session_files` already performs that lookup. [K, P-DOCS]
- A missing file means the session no longer exists. A filesystem read error, a launch failure with another message, or a timeout is temporary. Do not let `--session-id` create a new session before that distinction is made. [P-DOCS, K]

### 7. Corrections found while writing the reader

The reader in `src/kanbeat/pi_history.py` follows the entry tree and found these additions to the traversal rules:

- The file has no separate leaf marker. The reader treats the last valid appended entry as the leaf, follows `parentId` to the root and reverses the chain, so entries on other branches are not exported. A session where the user navigated back without sending a message cannot be read exactly from the file alone: the last appended entry can belong to the previously active branch. [P-DOCS, K]
- One message entry can produce several events: assistant text, hidden reasoning, tool calls and tool results may share the same record. The first exported event uses the bare entry `id`; later events from the same entry use `id:<index>` so event keys stay unique. The file line index is the position. [P-DOCS, K]
- Every v2/v3 entry requires `parentId`. A missing or broken parent chain, a duplicate entry id, malformed JSON, a missing or mismatched session header, or a file that changes during the read makes the result available but incomplete rather than silently dropping entries. [P-DOCS, K]
- `bashExecution` messages are direct shell commands, not model tool results. The reader exports them as a labelled `bash` tool call with the command and visible output. [P-DOCS]
- System messages carry prompt sections and tool declarations, not conversation, so the reader skips them. `compaction`, `branch_summary`, `context_edit` and `custom_message` entries, plus `custom`, `branchSummary` and `compactionSummary` message roles, are reported as unsupported categories. Bookkeeping entry types such as `model_change`, `thinking_level_change`, `usage`, `label` and `session_info` are skipped. [P-DOCS]
- An import is recognized only when a user message is a complete import envelope: the start marker must be at the beginning and the matching end marker at the end. A bare marker quoted inside a longer message is not an import. [K]

## What later tickets should rely on

- **Codex:** `thread/items/list` with item ids and `turn/start` with `clientUserMessageId`; treat `thread/inject_items` as an unverified silent alternative. [C-SCHEMA, C-SRC, C-TEST]
- **OpenCode:** `GET /session/{id}/message` paging with `limit`/`before`; the reader in `src/kanbeat/opencode_history.py` distinguishes available, missing and unreachable history; `POST /session/{id}/message` with `messageID`, `noReply: true`, a `system` note and a text part. [O-SRC, K]
- **Claude:** transcript records with `uuid`/`parentUuid`; delivery through `claude --resume … -p` and a context file; recognize imports by marker and then record the user record's `uuid`. [CL-FILES, CLI]
- **Pi:** session entries with `id`/`parentId`; the reader in `src/kanbeat/pi_history.py` distinguishes available, missing and unreachable history, follows the last appended `parentId` chain and reports unsupported categories; delivery through the RPC `prompt` or `pi -p --session-id`; recognize imports by marker and then record the user entry's `id`. [P-DOCS, K]

## Unverified because it needs a live model

These were not tested because the ticket forbids starting a harness session:

1. Claude Code and Pi marker survival after a real prompt. The file formats store user text as supplied, but no live run confirmed byte-for-byte preservation.
2. The exact transcript record written by `claude --resume … -p`, including whether the user record is appended before the process exits and whether Claude normalizes the prompt.
3. Pi's handling of a marker that resembles a slash command or template; avoid a leading `/` and recheck live.
4. Whether Codex `thread/inject_items` makes the injected item visible through `thread/items/list` and `thread/items/read`. The source test only checks the rollout and the next model request.
5. The actual server body-size limit for an OpenCode `POST /session/{id}/message`, and for a Pi RPC `prompt` or one-shot prompt.
6. Whether OpenCode's `noReply` message is immediately visible in a subsequent paged `GET /session/{id}/message` after a restart. The source stores it before returning, but this was not exercised.
7. Non-text fidelity: images, audio, tool attachments, signatures and provider-specific metadata are intentionally excluded or reduced to references; no round trip was tested.
8. Any behavior that differs between harness releases. The versions above are the only ones inspected.

## Sources

- **[C-SCHEMA]** `codex app-server generate-json-schema --experimental` (Codex CLI 0.166.0), `codex_app_server_protocol.v2.schemas.json`, definitions `ThreadItemsListParams`, `ThreadItemsListResponse`, `ThreadTurnsListParams`, `ThreadTurnsListResponse`, `ThreadReadParams`, `ThreadInjectItemsParams`, `ThreadResumeParams`, `TurnStartParams`, `ThreadItem`, `Turn`, `Thread`, `UserInput`.
- **[C-SRC]** `openai/codex` at tag `rust-v0.166.0`, commit `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`: `codex-rs/app-server/src/request_processors/thread_processor.rs`, `codex-rs/app-server/src/request_processors/turn_processor.rs`, `codex-rs/app-server-protocol/src/protocol/v2/thread.rs`, `.../v2/turn.rs`, `.../v2/item.rs`, `codex-rs/core/src/codex_thread.rs`, `codex-rs/protocol/src/user_input.rs`.
- **[C-TEST]** `openai/codex` at the same commit: `codex-rs/app-server/tests/suite/v2/thread_inject_items.rs`.
- **[C-FILES]** Local rollout files under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, inspected structurally (top-level keys, `type`, `payload.type`, `ordinal` contiguity) without reading conversation content.
- **[O-SRC]** `sst/opencode` at tag `v1.19.3`, commit `e3c4eb242585dcd77be1a867391b7c25fd47ce52`: `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts`, `.../handlers/session.ts`, `packages/opencode/src/session/message-v2.ts`, `packages/opencode/src/session/prompt.ts`, `packages/opencode/src/session/processor.ts`, `packages/opencode/src/session/run-state.ts`, `packages/opencode/src/session/status.ts`, `packages/opencode/src/session/session.ts`, `packages/opencode/src/session/llm/request.ts`, `packages/opencode/src/id/id.ts`, `packages/opencode/src/session/schema.ts`, `packages/schema/src/v1/session.ts`.
- **[O-DB]** Local `~/.local/share/opencode/opencode.db` schema (`message`, `part`, `session` tables), read as schema only.
- **[CLI]** `claude --help` on Claude Code 2.1.316, especially `--resume`, `--continue`, `--session-id`, `--fork-session`, `--print` and `--input-format`.
- **[CL-FILES]** Transcript structure observed under `~/.claude/projects/<munged cwd>/<session-id>.jsonl` on Claude Code 2.1.316: record types, keys, block types, UUID uniqueness, ordering and turn markers, without reading conversation content.
- **[CL-BIN]** Error strings found in the installed Claude Code 2.1.316 binary: `No conversation found with session ID:` and `No conversation found to continue`.
- **[P-DOCS]** Pi 1.4.0 package documentation installed at `~/.nvm/versions/node/v24.21.0/lib/node_modules/@earendil-works/pi-coding-agent/docs/`: `session-format.md`, `sessions.md`, `message-types.md`, `rpc-commands.md`, `rpc.md`, `json.md`, `cli-integration.md` and `cli.md`.
- **[P-FILES]** Local Pi session files under `~/.pi/agent/sessions/--<path>--/*.jsonl`, inspected structurally without reading conversation content.
- **[K]** Kanbeat source: `src/kanbeat/codex_host.py`, `src/kanbeat/opencode_host.py`, `src/kanbeat/claude_runtime.py`, `src/kanbeat/session_groups.py`.