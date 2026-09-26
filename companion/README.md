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
- [ ] FR-A3/A4 AX reader · FR-A5 WebSocket · FR-A6/A7 pet levels · FR-A8 bubble · FR-A9 Fix it ·
      FR-A10 onboarding · FR-A11 mock mode

## AX tree findings (Day-One Test, PRD §19 Q5)

- Claude bundle ID: `com.anthropic.claudefordesktop` (tested on Claude 2.9939.2)
- AX reading: works after setting `AXManualAccessibility = true` on the app element
- User vs assistant message rule (observed in the Code tab, Claude 2.9939.2 — confirm in a Chat-tab
  conversation): each message starts with a screen-reader-only `AXHeading` (DOM class `sr-only`)
  whose title is `"You said: …"` (user) or `"Claude responded: …"` (assistant). User messages are an
  `AXGroup[AXDocumentArticle]` titled `"Message N"`; an assistant reply can span several sibling
  `message-row` groups until the next heading. Body text is in descendant `AXStaticText` values.
  _TODO: encode in `Reigns/Config/AXRules.json`._
- Message input element: `AXTextArea` with DOM classes `tiptap ProseMirror` (the focused composer).

Run the test again: `swift Tools/day_one_test.swift` (read-only) or add `--paste` (never presses
Enter). Its dump file is git-ignored because it contains chat text.
