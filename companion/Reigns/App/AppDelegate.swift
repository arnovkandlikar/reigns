import AppKit
import ApplicationServices

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let state = AppState.shared
    private var rules = AXRules.fallback
    private var monitor: ClaudeAppMonitor!
    private var tracker: ClaudeWindowTracker!
    private let dockTracker = DockTracker()
    private var watcher: ConversationWatcher!
    private var engine: EngineClient!
    private var inserter: ComposerInserter!
    private let voice = VoicePlayer()
    /// Assistant replies sent to the engine and still waiting for verdicts (drives the thinking bubble).
    private var scanning: [String: DispatchWorkItem] = [:]
    private static let scanTimeout: TimeInterval = 30
    private var pet: PetPanelController!
    private var frontmostClaude: NSRunningApplication?

    func applicationDidFinishLaunching(_ notification: Notification) {
        rules = AXRules.load()

        // FR-A10 (onboarding sheet) comes later; for now just trigger the system prompt.
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        state.axTrusted = AXIsProcessTrustedWithOptions(options)
        if !state.axTrusted {
            Log.app.warning("Not AX-trusted; window tracking disabled until permission is granted")
        }

        pet = PetPanelController(inset: rules.petInsetPx)
        tracker = ClaudeWindowTracker(pollInterval: Double(rules.windowPollMs) / 1000)
        tracker.onFrameChange = { [weak self] frame in self?.pet.claudeWindowChanged(frame) }
        dockTracker.onChange = { [weak self] frame in self?.pet.dockChanged(frame) }

        engine = EngineClient(url: EngineClient.configuredURL()) { [weak self] in
            self?.claudeVersion() ?? "unknown"
        }
        engine.onStatus = { [weak self] status in self?.state.engineStatus = status }
        engine.onHeat = { [weak self] heat in self?.pet.apply(heat) }
        engine.onBubble = { [weak self] bubble in self?.pet.apply(bubble) }
        engine.onVerdicts = { [weak self] verdicts in
            self?.pet.apply(verdicts)
            self?.finishScan(verdicts.messageID)
        }
        engine.onError = { [weak self] _ in self?.clearScans() }
        engine.onVoice = { [weak self] line in
            guard let self, !self.state.isVoiceMuted, !self.state.isPaused else { return }
            self.voice.play(line)
        }
        pet.onDisagree = { [weak self] claimID in self?.engine.sendDisagree(claimID: claimID) }
        inserter = ComposerInserter(composerDOMClass: rules.composerDOMClass)
        pet.onFixIt = { [weak self] correction, mode in self?.fixIt(correction, mode: mode) }
        engine.start()

        watcher = ConversationWatcher(reader: ConversationReader(rules: rules.conversation))
        watcher.onMessage = { [weak self] completed in
            self?.engine.sendMessage(completed)
            if completed.message.role == .assistant { self?.startScan(completed.id) }
        }
        watcher.onConversationChange = { [weak self] chatKey in
            self?.engine.startNewSession(chatKey: chatKey)
            self?.clearScans()
            self?.pet.resetForNewConversation()
        }

        monitor = ClaudeAppMonitor(claudeBundleID: rules.claudeBundleID)
        monitor.onChange = { [weak self] app in
            self?.frontmostClaude = app
            self?.apply()
        }
        monitor.start()
        #if DEBUG
        observeDebugNotifications()
        #endif
        Log.app.info("Reigns companion started; watching \(self.rules.claudeBundleID, privacy: .public)")
    }

    #if DEBUG
    /// Sample heat per level so the badge matches the preview (§9 level ranges).
    private static let previewHeat = [0, 20, 45, 70, 95]

    /// Lets us see each level's peek height before heat.update is wired up (FR-A5/A6).
    func previewLevel(_ level: Int) {
        pet.update(level: level, heat: Self.previewHeat[level], bubble: .preview(level: level),
                   unverified: [0, 2, 3, 1, 2][level])
    }

    /// Reads the conversation off the main thread and logs a summary (FR-A3 check).
    func logConversation() {
        guard let pid = frontmostClaude?.processIdentifier else {
            Log.ax.info("Claude isn't frontmost; nothing to read")
            return
        }
        let reader = ConversationReader(rules: rules.conversation)
        DispatchQueue.global(qos: .userInitiated).async {
            let start = Date()
            let messages = reader.read(pid: pid)
            let ms = Int(Date().timeIntervalSince(start) * 1000)
            Log.ax.info("Read \(messages.count) messages in \(ms) ms")
            for message in messages.suffix(6) {
                Log.ax.debug("[\(message.position)] \(message.role.rawValue, privacy: .public): \(message.text.prefix(80))")
            }
        }
    }

    /// Show the thinking bubble for 4 s, as if a reply were being scanned.
    func previewScanning() {
        pet.setScanning(true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            MainActor.assumeIsolated { self?.pet.setScanning(!(self?.scanning.isEmpty ?? true)) }
        }
    }

    /// Recovered for 3 s (what heat.update.recovered does after a verified fix), then Calm.
    func previewRecovered() {
        pet.update(level: 0, heat: 0, bubble: .allClear, recovered: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            self?.pet.update(level: 0, heat: 0, bubble: .allClear, recovered: false)
        }
    }

    /// Debug hook: `sh companion/Tools/next_level.sh` steps the pet 0→1→2→3→4→0.
    private func observeDebugNotifications() {
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("app.reigns.debug.nextLevel"), object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.previewLevel((self.pet.model.level + 1) % Self.previewHeat.count)
            }
        }
    }
    #endif

    // MARK: - Scanning indicator (thinking bubble)

    private func startScan(_ messageID: String) {
        let timeout = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.finishScan(messageID) }  // never think forever
        }
        scanning[messageID]?.cancel()
        scanning[messageID] = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.scanTimeout, execute: timeout)
        pet.setScanning(true)
    }

    private func finishScan(_ messageID: String) {
        scanning.removeValue(forKey: messageID)?.cancel()
        pet.setScanning(!scanning.isEmpty)
    }

    private func clearScans() {
        scanning.values.forEach { $0.cancel() }
        scanning.removeAll()
        pet.setScanning(false)
    }

    /// FR-A9: paste the correction into Claude's message box (never sends it), then tell the engine.
    private func fixIt(_ correction: Correction, mode: ComposerInserter.Mode?) {
        guard let app = frontmostClaude
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: rules.claudeBundleID).first
        else { return }

        var chosen = mode ?? .replace
        if mode == nil {
            switch inserter.state(pid: app.processIdentifier) {
            case .hasText:
                pet.askReplaceOrAdd(correction)  // "Replace or add to your text?"
                return
            case .empty, .notFound:
                chosen = .replace
            }
        }

        inserter.insert(correction.text, into: app, mode: chosen) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .inserted:
                Log.pet.info("Fix it: prompt inserted (\(correction.promptType.rawValue, privacy: .public))")
                self.engine.sendCorrectionInserted(correctionID: correction.correctionID)
                self.pet.closeBubbleAfterFix()
            case .copiedToClipboard:
                self.pet.showFixNote("Couldn't reach Claude's message box. The fix is on your clipboard: click the box and press ⌘V.")
                self.engine.sendCorrectionInserted(correctionID: correction.correctionID)
            }
        }
    }

    private func claudeVersion() -> String {
        let app = frontmostClaude
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: rules.claudeBundleID).first
        return app?.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary?["CFBundleShortVersionString"] as? String }
            ?? "unknown"
    }

    func togglePause() {
        state.isPaused.toggle()
        apply()
    }

    /// FR-A1: pet is visible only while Claude is frontmost (and Reigns isn't paused).
    private func apply() {
        state.axTrusted = AXIsProcessTrusted()
        guard let app = frontmostClaude, !state.isPaused else {
            tracker.stop()
            dockTracker.stop()
            watcher.stop()  // also honours Pause (PRD §18): nothing is read while paused
            pet.wantsVisible = false
            return
        }
        ConversationReader.enableAccessibility(pid: app.processIdentifier)  // FR-A3
        tracker.start(pid: app.processIdentifier)
        dockTracker.start()
        watcher.start(pid: app.processIdentifier)  // FR-A4
        pet.wantsVisible = true
    }
}
