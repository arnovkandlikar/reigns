# Reigns Companion (Role A — Swift / macOS)

The pet that sits on the Claude desktop app window. Owner: Role A. See `PRD` §6, §7.1, §10, §12.

## Build & run

Requires Xcode 16+ (project uses folder-synced groups — just drop new `.swift` files into
`Reigns/`, no project-file edits needed, so `project.pbxproj` rarely changes).

```bash
export REIGNS_SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)"   # optional, see below
sh companion/run.sh     # builds, kills any running copy, relaunches
```

Or open `Reigns.xcodeproj` in Xcode and press Run.

- Runs as a menu-bar app (eye icon, no Dock icon). Menu: **Pause Reigns** (PRD §18) and **Quit**.
- Needs **Accessibility** permission (System Settings › Privacy & Security › Accessibility).
- Without `REIGNS_SIGN_IDENTITY` the build is ad-hoc signed and macOS forgets the permission after
  every rebuild. With your Apple Development identity set, grant it once and it sticks.
- Logs: `log stream --predicate 'subsystem == "app.reigns"'`

## Layout

| Folder | Contents |
|---|---|
| `Reigns/App` | App entry, menu bar, AppDelegate wiring, logging |
| `Reigns/AX` | FR-A1 Claude-frontmost monitor, FR-A2 window tracker (later: FR-A3/A4 reader) |
| `Reigns/Pet` | Floating panel, corner logic, pet view + view model |
| `Reigns/Config` | `AXRules.json` — all Claude element-matching rules (PRD R8) |
| `Tools` | `day_one_test.swift` (Day-One Test) |

## Status

- [x] FR-A1 show/hide with Claude (fade 250 ms)
- [x] FR-A2 non-activating floating panel, bottom-right inset 24 px, follows window (250 ms poll),
      drag to another corner (remembered)
- [x] FR-A3 conversation reader (Chat + Code tabs)
- [x] FR-A8 speech bubble UI (Fix it / Details / I disagree / Dismiss) with sample content;
      buttons log until FR-A5/FR-A9 land
- [x] FR-A4 completion detection (`Reigns/AX/ConversationWatcher.swift`): polls every 0.5 s (backs
      off for slow reads), a message is complete after 1.5 s unchanged and while no Stop control is
      showing; `message_id` = SHA-1 of `"position:first 200 chars"`, never sent twice. Conversations
      already on screen are history; a chat that sat empty is reported from message 0. Switches are
      detected by page title / mismatched messages; old messages scrolling into view are history.
      Watch it: `log stream --info --predicate 'subsystem == "app.reigns" AND category == "ax"'`
- [x] FR-A5 engine WebSocket (`Reigns/Networking/EngineClient.swift`): `ws://127.0.0.1:8765/ws`
      (override with `REIGNS_ENGINE_URL`), reconnect 1 s → 2 s → 5 s, `session.start` first on every
      connection, queues while offline, new session per conversation. Menu shows engine status.
- [x] Only the Chat tab is read; the Code tab is skipped (`ignored_mode_titles`)
- [x] FR-A6 expressions (`Reigns/Pet/PetView.swift`): Calm / Curious / Concerned / Alarmed /
      Meltdown / Recovered per §10 (separate eye, eyebrow and mouth views; blink, breathing, tilt,
      fidget, tremble, shake, hop). Driven only by `heat.update` (`level`, `recovered`), spring-animated.
      Debug: menu › Preview Level › Level 0–4 / Recovered.
- [x] FR-A7 accessories: always-visible heat badge; "?" badge with unverified (amber) count at
      levels 1–2; sweat drop + waving red flag at ≥ 3; nostril steam + "START FRESH?" sign at 4
      (bubble opens above the sign).
- [ ] · FR-A6/A7 pet levels · FR-A9 Fix it ·
      FR-A10 onboarding · FR-A11 mock mode

## AX tree findings (Day-One Test, PRD §19 Q5)

- Claude bundle ID: `com.anthropic.claudefordesktop` (tested on Claude 2.9939.2)
- AX reading: works after setting `AXManualAccessibility = true` on the app element
- User vs assistant message rule (FR-A3, implemented in `Reigns/AX/ConversationReader.swift`,
  strings in `Reigns/Config/AXRules.json` → `conversation`). Verified in both the Chat and Code
  tabs on Claude 2.9939.2 (text, lists, headings and code blocks come through intact).
  - Each message starts with a screen-reader-only `AXHeading` titled `"You said: …"` (user) or
    `"Claude responded: …"` (assistant). Only real headings count, so quoted text can't fake one.
  - Each message lives in an `AXGroup[AXDocumentArticle]` whose description is `"Message N"`
    (Code) or `"Message N of M"` (Chat), 1-based → `position = N - 1`. Claude virtualizes the list (only nearby messages are in the
    tree), so positions come from this label, not from counting.
  - User text = static text inside its own article. Assistant text = static text from its heading to
    the next heading (replies continue in sibling rows). A reply still streaming has no heading yet
    and is ignored.
  - Only text inside the smallest element containing every heading is read, which keeps the
    sidebar, composer and footer out. Buttons, toolbars (Copy, timestamps), tool pills and text
    fields are skipped, plus reply widgets matched by DOM id/class/subrole: visuals
    (`mcp-app-*`), tool-step summaries (`*-label`, `AXApplicationStatus`) and file cards
    (`group/artifact-block`).
  - Code blocks: highlighter tokens inside `AXCodeStyleGroup` are joined verbatim (they carry
    their own `\n`); everywhere else, directly adjacent text runs mean a line break.
  - Speed: ~25 ms for the visible window of messages. A long non-virtualized conversation (~200
    messages) took ~3 s, so FR-A4 must read off the main thread and not too often.
  - Dev harness: `sh Tools/read_conversation.sh [--full]` prints what the reader extracts.
- Message input element: `AXTextArea` with DOM classes `tiptap ProseMirror` (the focused composer).

Run the test again: `swift Tools/day_one_test.swift` (read-only) or add `--paste` (never presses
Enter). Its dump file is git-ignored because it contains chat text.
