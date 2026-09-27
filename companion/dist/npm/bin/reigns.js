#!/usr/bin/env node
// reigns-work: installs and manages Reigns on macOS.
//   npx reigns-work install    app → ~/Applications, engine → ~/.reigns, auto-start at login
//   npx reigns-work update     newer app + engine, keeps your keys
//   npx reigns-work keys       change API keys
//   npx reigns-work start|stop|restart|status
//   npx reigns-work uninstall
// No dependencies: uses macOS tools (ditto, tar, launchctl, python3).
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const readline = require('readline');
const { spawnSync } = require('child_process');

const PKG = path.resolve(__dirname, '..');
const PAYLOAD = path.join(PKG, 'payload');
const VERSION = require(path.join(PKG, 'package.json')).version;

// REIGNS_HOME / REIGNS_APPS let you try the installer in a scratch folder.
const HOME = os.homedir();
const BASE = process.env.REIGNS_HOME || path.join(HOME, '.reigns');
const APPS = process.env.REIGNS_APPS || path.join(HOME, 'Applications');
const APP = path.join(APPS, 'Reigns.app');
const SRC = path.join(BASE, 'src');
const VENV = path.join(BASE, 'venv');
const ENV_FILE = path.join(BASE, '.env');
const LOGS = path.join(BASE, 'logs');
const RUNNER = path.join(BASE, 'run-engine.sh');
const LABEL = 'work.reigns.engine';
// A scratch REIGNS_HOME (testing) keeps the LaunchAgent out of your real login items too.
const PLIST = process.env.REIGNS_HOME
  ? path.join(BASE, `${LABEL}.plist`)
  : path.join(HOME, 'Library', 'LaunchAgents', `${LABEL}.plist`);
const PORT = 8765;

// Reigns' horse (Charlie) and unicorn (Marley) ElevenLabs voices.
const VOICES = { REIGNS_VOICE_ID: 'LysucvtFmzi1NVAE0rKp', REIGNS_UNICORN_VOICE_ID: '9QPzUjm1evjwY2ENQBKU' };

const args = process.argv.slice(2);
const flags = new Set(args.filter((a) => a.startsWith('--')));
const command = args.find((a) => !a.startsWith('--')) || 'help';

// ---------------------------------------------------------------- helpers

const say = (msg) => console.log(msg);
const step = (msg) => console.log(`\n→ ${msg}`);
const fail = (msg) => {
  console.error(`\n✖ ${msg}`);
  process.exit(1);
};

function run(cmd, cmdArgs, opts = {}) {
  const r = spawnSync(cmd, cmdArgs, { stdio: opts.quiet ? 'pipe' : 'inherit', encoding: 'utf8', ...opts });
  if (r.status !== 0 && !opts.allowFail) {
    fail(`${cmd} ${cmdArgs.join(' ')} failed${r.stderr ? `:\n${r.stderr}` : ''}`);
  }
  return r;
}

// One reader for every question: separate readers would each swallow buffered input (e.g. when
// answers are piped in), leaving later questions waiting forever.
let reader = null;
let readerClosed = false;
const pendingAnswers = [];

function getReader() {
  if (!reader) {
    reader = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: process.stdin.isTTY });
    reader.on('close', () => {
      readerClosed = true;
      while (pendingAnswers.length) pendingAnswers.shift()('');  // input ended: answer blank
    });
  }
  return reader;
}

function ask(question, { secret = false } = {}) {
  return new Promise((resolve) => {
    if (readerClosed) return resolve('');
    const rl = getReader();
    const write = rl._writeToOutput;
    if (secret) {
      // Don't echo keys to the terminal.
      rl._writeToOutput = (s) => {
        if (s.includes(question)) rl.output.write(s);
      };
    }
    pendingAnswers.push(resolve);
    rl.question(question, (answer) => {
      pendingAnswers.splice(pendingAnswers.indexOf(resolve), 1);
      if (secret) {
        rl._writeToOutput = write;
        if (process.stdin.isTTY) process.stdout.write('\n');
      }
      resolve(answer.trim());
    });
  });
}

function closeReader() {
  if (reader && !readerClosed) reader.close();
}

function uid() {
  return String(process.getuid());
}

function healthy() {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port: PORT, path: '/health', timeout: 1500 }, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

// ---------------------------------------------------------------- checks

function checkMac() {
  if (process.platform !== 'darwin') fail('Reigns runs on macOS only.');
  const major = parseInt(run('sw_vers', ['-productVersion'], { quiet: true }).stdout.split('.')[0], 10);
  if (major < 14) fail('Reigns needs macOS 14 (Sonoma) or newer.');
  for (const f of ['Reigns.zip', 'engine.tar.gz']) {
    if (!fs.existsSync(path.join(PAYLOAD, f))) fail(`This package is missing payload/${f} (was it built with build_release.sh?).`);
  }
}

function findPython() {
  const candidates = ['python3.13', 'python3.12', 'python3.11', '/opt/homebrew/bin/python3', '/usr/local/bin/python3', 'python3'];
  for (const py of candidates) {
    const r = spawnSync(py, ['-c', 'import sys; print(sys.version_info >= (3, 11))'], { encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim() === 'True') return py;
  }
  fail('Reigns needs Python 3.11 or newer. Install it with:\n\n    brew install python@3.12\n\n(or from python.org), then run this again.');
}

// ---------------------------------------------------------------- engine

function installEngineSource() {
  step('Unpacking the engine');
  const fresh = `${SRC}.new`;
  fs.rmSync(fresh, { recursive: true, force: true });
  fs.mkdirSync(fresh, { recursive: true });
  run('tar', ['-xzf', path.join(PAYLOAD, 'engine.tar.gz'), '-C', fresh]);
  fs.mkdirSync(path.join(fresh, 'engine', 'logs'), { recursive: true });
  fs.rmSync(SRC, { recursive: true, force: true });
  fs.renameSync(fresh, SRC);
}

function installEngineDeps(python) {
  step('Installing the engine’s Python packages (first time takes a minute or two)');
  if (!fs.existsSync(path.join(VENV, 'bin', 'python'))) run(python, ['-m', 'venv', VENV]);
  const pip = path.join(VENV, 'bin', 'pip');
  run(pip, ['install', '--quiet', '--upgrade', 'pip']);
  run(pip, ['install', '--quiet', path.join(SRC, 'engine')]);
}

async function writeKeys({ force = false } = {}) {
  if (fs.existsSync(ENV_FILE) && !force) {
    say('\nKeeping your saved API keys (change them with: npx reigns-work keys).');
    return;
  }
  step('API keys (saved only on this Mac, in ~/.reigns/.env)');
  say('  Anthropic is required. Get one at https://console.anthropic.com (you pay Anthropic for your own use).');
  let anthropic = '';
  while (!anthropic) {
    anthropic = await ask('  Anthropic API key: ', { secret: true });
    if (!anthropic && readerClosed) fail('An Anthropic API key is required.');
  }
  say('  Optional, press Enter to skip:');
  const tavily = await ask('  Tavily API key (web fact checks, free tier at tavily.com): ', { secret: true });
  const eleven = await ask('  ElevenLabs API key (the horse talks, free tier at elevenlabs.io): ', { secret: true });

  const lines = [
    '# Reigns engine settings. Written by `npx reigns-work install` / `keys`.',
    `ANTHROPIC_API_KEY=${anthropic}`,
    `TAVILY_API_KEY=${tavily}`,
    `ELEVENLABS_API_KEY=${eleven}`,
    ...(eleven ? Object.entries(VOICES).map(([k, v]) => `${k}=${v}`) : []),
    `REIGNS_DB=${path.join(BASE, 'reigns.db')}`,
    `REIGNS_MEMORY_DB=${path.join(BASE, 'reigns_memory.db')}`,
    `REIGNS_PORT=${PORT}`,
    '',
  ];
  fs.mkdirSync(BASE, { recursive: true });
  fs.writeFileSync(ENV_FILE, lines.join('\n'), { mode: 0o600 });
}

function writeRunner() {
  // Loads ~/.reigns/.env (trimming stray spaces around values) and starts the engine.
  const script = `#!/bin/sh
cd "${path.join(SRC, 'engine')}" || exit 1
while IFS='=' read -r k v; do
  case "$k" in ''|\\#*) continue;; esac
  v="$(printf '%s' "$v" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -n "$v" ] && export "$k=$v"
done < "${ENV_FILE}"
exec "${path.join(VENV, 'bin', 'uvicorn')}" app.main:app --host 127.0.0.1 --port "\${REIGNS_PORT:-${PORT}}"
`;
  fs.mkdirSync(LOGS, { recursive: true });
  fs.writeFileSync(RUNNER, script, { mode: 0o755 });
}

function writeLaunchAgent() {
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>${RUNNER}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${path.join(LOGS, 'engine.log')}</string>
  <key>StandardErrorPath</key><string>${path.join(LOGS, 'engine.log')}</string>
</dict>
</plist>
`;
  fs.mkdirSync(path.dirname(PLIST), { recursive: true });
  fs.writeFileSync(PLIST, plist);
}

function stopEngine() {
  run('launchctl', ['bootout', `gui/${uid()}/${LABEL}`], { quiet: true, allowFail: true });
}

async function startEngine() {
  if (!fs.existsSync(PLIST)) fail('The engine isn’t installed yet. Run: npx reigns-work install');
  stopEngine();
  run('launchctl', ['bootstrap', `gui/${uid()}`, PLIST], { quiet: true, allowFail: true });
  process.stdout.write('  Starting the engine');
  for (let i = 0; i < 90; i++) {
    if (await healthy()) {
      say(' — running.');
      return true;
    }
    process.stdout.write('.');
    await new Promise((r) => setTimeout(r, 1000));
  }
  say(`\n  The engine didn’t come up yet. Check the log: ${path.join(LOGS, 'engine.log')}`);
  return false;
}

// ---------------------------------------------------------------- app

function installApp() {
  step(`Installing Reigns.app into ${APPS}`);
  quitInstalledApp();
  fs.mkdirSync(APPS, { recursive: true });
  fs.rmSync(APP, { recursive: true, force: true });
  run('ditto', ['-x', '-k', path.join(PAYLOAD, 'Reigns.zip'), APPS]);
  // npm downloads aren't quarantined, but clear it in case the package came another way.
  run('xattr', ['-dr', 'com.apple.quarantine', APP], { quiet: true, allowFail: true });
}

/// Quit only the copy this installer manages (by its full path), never another Reigns such as a
/// developer build running from somewhere else.
function quitInstalledApp() {
  run('pkill', ['-f', path.join(APP, 'Contents', 'MacOS', 'Reigns')], { quiet: true, allowFail: true });
}

function openApp() {
  if (flags.has('--no-open')) return;
  run('open', [APP], { allowFail: true });
}

// ---------------------------------------------------------------- commands

async function install({ update = false } = {}) {
  checkMac();
  say(`Reigns ${VERSION} — ${update ? 'updating' : 'installing'}`);
  const python = findPython();
  say(`  Using ${python}`);
  if (update) stopEngine();
  installEngineSource();
  installEngineDeps(python);
  await writeKeys();
  writeRunner();
  writeLaunchAgent();
  installApp();
  if (!flags.has('--no-agent')) {
    step('Starting the engine (and at every login from now on)');
    await startEngine();
  }
  openApp();
  say(`
✔ Reigns is ${update ? 'updated' : 'installed'}.
  • The horse appears on your Claude desktop window (Chat tab).
  • First run: allow Reigns in System Settings › Privacy & Security › Accessibility
    (the welcome window walks you through it).
  • Change keys: npx reigns-work keys    • Status: npx reigns-work status
`);
}

async function status() {
  const appThere = fs.existsSync(APP);
  const agent = run('launchctl', ['print', `gui/${uid()}/${LABEL}`], { quiet: true, allowFail: true }).status === 0;
  const up = await healthy();
  say(`Reigns ${VERSION}`);
  say(`  App:     ${appThere ? APP : 'not installed'}`);
  say(`  Engine:  ${up ? `running on 127.0.0.1:${PORT}` : 'not responding'}${agent ? ' (auto-start on)' : ''}`);
  say(`  Keys:    ${fs.existsSync(ENV_FILE) ? ENV_FILE : 'not set'}`);
  say(`  Logs:    ${path.join(LOGS, 'engine.log')}`);
}

async function uninstall() {
  stopEngine();
  fs.rmSync(PLIST, { force: true });
  quitInstalledApp();
  fs.rmSync(APP, { recursive: true, force: true });
  say('Removed the app and the engine auto-start.');
  const answer = await ask(`Also delete ${BASE} (your API keys and history)? [y/N] `);
  if (answer.toLowerCase().startsWith('y')) {
    fs.rmSync(BASE, { recursive: true, force: true });
    say(`Deleted ${BASE}.`);
  } else {
    say(`Kept ${BASE}.`);
  }
  say('Tip: also remove Reigns from System Settings › Privacy & Security › Accessibility.');
}

function help() {
  say(`reigns-work ${VERSION} — the Reigns desktop horse for Claude (macOS)

  npx reigns-work install     install the app + engine (asks for your API keys)
  npx reigns-work update      update, keeping your keys
  npx reigns-work keys        change your API keys
  npx reigns-work start | stop | restart | status
  npx reigns-work uninstall`);
}

(async () => {
  switch (command) {
    case 'install': return install();
    case 'update': return install({ update: true });
    case 'keys':
      await writeKeys({ force: true });
      if (fs.existsSync(PLIST)) await startEngine();
      return;
    case 'start':
    case 'restart': return startEngine();
    case 'stop': stopEngine(); say('Engine stopped.'); return;
    case 'status': return status();
    case 'uninstall': return uninstall();
    default: return help();
  }
})()
  .then(closeReader)
  .catch((err) => fail(err.message));
