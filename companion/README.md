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
- [x] FR-A8 speech bubble complete (Fix it wired to FR-A9)
- [x] FR-A9 Fix it (`Reigns/AX/ComposerInserter.swift`): finds the message box (AXTextArea with DOM
      class `ProseMirror`), asks inline "Replace / Add to it" if it already has text, activates
      Claude, focuses the box, saves the clipboard, pastes with ⌘V, restores the clipboard after
      300 ms, sends `correction.inserted`. Never presses Enter. If the box can't be found the prompt
      is left on the clipboard and the bubble says to press ⌘V (PRD R2).
- [x] Scanning indicator: while the engine checks a reply Reigns sent (message.new → its
      verdicts.update; 30 s safety timeout), a thought bubble with a galloping horse shows above
      the pet. Debug: menu › Preview Level › Scanning (thinking).
- [x] FR-A10 onboarding (`Reigns/Onboarding/`): shown at launch whenever Accessibility permission
      is missing. Explains why it's needed (only reads Claude, only types on Fix it, never sends),
      opens the Accessibility pane, detects the grant within ~1 s, then closes and starts watching
      without a restart. Menu › Set Up Accessibility… reopens it (debug: Preview Level › Onboarding Window).
- [x] FR-A11 mock mode (`Reigns/Networking/MockEngine.swift`): `REIGNS_MOCK=1` (or `WITNESS_MOCK=1`),
      or debug menu › Preview Level › Mock Engine (fixtures). Loops through
      `shared/fixtures/scenarios/*.json` (demo scenarios first): each scenario is a fresh chat, each
      Claude reply shows the thinking bubble, then the recorded engine messages go through the normal
      EngineClient decoding path. No engine connection and no Claude reading while on.
      Run: `open --env REIGNS_MOCK=1 companion/build/Debug/Reigns.app`
- [x] "Click me!" callout above the pet when there's an issue the user hasn't opened yet
- [x] Bubble voice controls: replay the last spoken line, mute/unmute (same setting as the menu)
- [x] Characters (menu › Character): **Charlie** (default horse) or **Marley** (a unicorn: golden spiral horn, pastel rainbow mane, sparkles, pearly white coat at Calm/Recovered,
      eyelashes, rosy cheeks). Shared expressions, level colours and accessories;
      Marley has her own engine voice (`"character": "charlie"|"marley"` in `session.start`); remembered across launches (`Reigns/Pet/PetCharacter.swift`).
- [x] Language (menu › Language): English or Español, sent as `"language": "en"|"es"`. Switching
      language or character mid-chat sends `session.update` (same session: score, history and bubble
      stay; the engine re-sends the bubble in the new language). `session.start` carries both on
      every (re)connect.
- [x] Session Brief offer (`brief.offer`): a calm, non-alarming bubble ("Context refresh") with the
      offer's headline, its action button and Not now. Accepting pastes the refresh into Claude's message
      box with the Fix it flow (Replace / Add to it, clipboard restored, never presses Enter; no
      `correction.inserted`). A warning (level ≥ 2) replaces it; heat and animation are unchanged.
      Debug: Preview Level › Context Refresh Offer.
- [x] On-screen highlights (menu › Highlight Problems, on by default): red/amber claims of the current
      chat are marked on Claude's window by a click-through overlay (red fill + solid underline = likely
      wrong, orange + dashed = couldn't confirm). Exact text via AXBoundsForRange, one box per line,
      refreshed ~3×/s so it follows scrolling (`Reigns/AX/HighlightScanner.swift`, `Reigns/Pet/HighlightOverlay.swift`).
- [x] The pet's eyes follow the mouse pointer.
- [x] When the Dock lifts the pet, the whole horse shows (no cut-off edge above the Dock).
- [x] Switching chats (or to the Code tab) stops any voice line about the previous chat.
- [x] Per-chat memory: each chat's flagged claims (highlights, Details), bubble and last spoken line
      come back when you return to it (Replay works after switching back). Last 20 chats.
- [x] Window tracking follows a dragged/resized Claude window once per screen refresh (display link,
      120 Hz on ProMotion); the pet and highlight overlay move with it frame by frame.
- [x] The bubble shows every problem the engine sends (scrolls past 5).

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
