# reigns-work

**Reigns** is a little horse (or unicorn) that sits on your Claude desktop window, checks Claude's
replies for made-up facts, highlights them, and helps Claude fix them.

macOS 14+ · Python 3.11+ · the Claude desktop app

## Install

```bash
npx reigns-work install
```

It installs `Reigns.app` into `~/Applications`, sets up the checking engine in `~/.reigns`
(starts automatically at login), and asks for your API keys:

- **Anthropic** (required): https://console.anthropic.com. You pay Anthropic for your own usage.
- **Tavily** (optional, free tier): better web fact checks.
- **ElevenLabs** (optional, free tier): the horse talks.

Keys are stored only on your Mac (`~/.reigns/.env`). On first launch, allow Reigns in
**System Settings › Privacy & Security › Accessibility**; the welcome window walks you through it.

## Commands

```bash
npx reigns-work update      # newer version, keeps your keys
npx reigns-work keys        # change API keys
npx reigns-work status      # is everything running?
npx reigns-work stop | start | restart
npx reigns-work uninstall
```

Reigns only reads the Claude app, only types when you click **Fix it**, and never sends messages.
