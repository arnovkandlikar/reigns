import AppKit
import SwiftUI
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
    /// Last line the engine spoke in this chat, for the bubble's replay button.
    private var lastVoice: VoicePlay?
    /// What the pet found in each chat (keyed like the engine's per-chat heat: the message_id of the
    /// chat's first message), so flipping back restores its highlights, bubble and Replay line.
    private struct ChatMemory {
        var claims: [ClaimVerdict] = []
        var bubble: BubbleContent?
        var voice: VoicePlay?
        /// Which reply (conversation position) each claim was flagged in.
        var claimPositions: [String: Int] = [:]
        /// Replies up to this position were covered by a Fix it: their claims aren't highlighted.
        var fixBoundary: Int?
        var disagreed: Set<String> = []
    }
    private var chatMemory: [String: ChatMemory] = [:]
    private var chatOrder: [String] = []
    private var currentChatKey: String?
    /// Position of every message sent to the engine in this chat, by message_id.
    private var messagePositions: [String: Int] = [:]
    /// Claim → position of the reply it was flagged in (current chat).
    private var claimPositions: [String: Int] = [:]
    /// Highest position covered by the last Fix it (current chat).
    private var fixBoundary: Int?
    private static let rememberedChats = 20
    private let onboarding = OnboardingWindow()
    private let highlightOverlay = HighlightOverlay()
    private var highlightScanner: HighlightScanner!
    private var highlightTimer: Timer?
    private var highlightBusy = false
    /// Latest frame of Claude's window (Cocoa), from the tracker.
    private var claudeWindowFrame: CGRect?
    private var mock: MockEngine!
    /// Assistant replies sent to the engine and still waiting for verdicts (drives the thinking bubble).
    private var scanning: [String: DispatchWorkItem] = [:]
    private static let scanTimeout: TimeInterval = 30
    private var pet: PetPanelController!
    private var frontmostClaude: NSRunningApplication?

    func applicationDidFinishLaunching(_ notification: Notification) {
        rules = AXRules.load()

        // FR-A10: explain the permission before asking for it (instead of the bare system prompt).
        state.axTrusted = AXIsProcessTrusted()
        onboarding.onTrusted = { [weak self] in
            self?.state.axTrusted = true
            self?.apply()
        }
        if !state.axTrusted {
            Log.app.warning("Not AX-trusted; showing onboarding")
            onboarding.show()
        }

        pet = PetPanelController(inset: rules.petInsetPx)
        tracker = ClaudeWindowTracker(pollInterval: Double(rules.windowPollMs) / 1000)
        tracker.onFrameChange = { [weak self] frame in
            self?.claudeWindowFrame = frame
            self?.pet.claudeWindowChanged(frame)
            self?.highlightOverlay.follow(frame)  // marks move with the window, frame by frame
        }
        dockTracker.onChange = { [weak self] frame in self?.pet.dockChanged(frame) }

        engine = EngineClient(url: EngineClient.configuredURL()) { [weak self] in
            self?.claudeVersion() ?? "unknown"
        }
        engine.onStatus = { [weak self] status in self?.state.engineStatus = status }
        engine.onHeat = { [weak self] heat in self?.pet.apply(heat) }
        engine.onBubble = { [weak self] bubble in
            guard let self else { return }
            self.pet.apply(bubble)
            self.remember { $0.bubble = bubble }
        }
        engine.onVerdicts = { [weak self] verdicts in
            guard let self else { return }
            self.pet.apply(verdicts)
            if let position = self.messagePositions[verdicts.messageID] {
                for claim in verdicts.claims { self.claimPositions[claim.claimID] = position }
            }
            self.remember {
                $0.claims = self.pet.model.claims
                $0.claimPositions = self.claimPositions
            }
            self.finishScan(verdicts.messageID)
        }
        engine.onError = { [weak self] _ in self?.clearScans() }
        engine.onBriefOffer = { [weak self] offer in self?.pet.showBriefOffer(offer) }
        pet.onBriefAccept = { [weak self] offer, mode in self?.pasteBrief(offer, mode: mode) }
        pet.onReplayVoice = { [weak self] in
            guard let self, let line = self.lastVoice else {
                Log.net.info("Replay: nothing to replay in this chat")
                return
            }
            Log.net.info("Replay")
            self.voice.play(line)  // an explicit replay plays even when muted
        }
        pet.onToggleMute = { [weak self] in
            guard let self else { return }
            self.state.isVoiceMuted.toggle()
            if self.state.isVoiceMuted { self.voice.stop() }
        }
        engine.onVoice = { [weak self] line in
            self?.lastVoice = line
            self?.pet.model.hasVoiceLine = true
            self?.remember { $0.voice = line }
            guard let self, !self.state.isVoiceMuted, !self.state.isPaused else { return }
            self.voice.play(line)
        }
        pet.onDisagree = { [weak self] claimID in
            guard let self else { return }
            self.engine.sendDisagree(claimID: claimID)
            self.remember { $0.disagreed = self.pet.model.disagreedClaimIDs }
        }
        inserter = ComposerInserter(composerDOMClass: rules.composerDOMClass)
        pet.onFixIt = { [weak self] correction, mode in self?.fixIt(correction, mode: mode) }
        mock = MockEngine(engine: engine)
        mock.onNewChat = { [weak self] _ in
            self?.voice.stop()
            self?.clearScans()
            self?.pet.resetForNewConversation()
            self?.lastVoice = nil
            self?.currentChatKey = nil  // mock scenarios are throwaway chats
        }
        mock.onScanning = { [weak self] scanning in self?.pet.setScanning(scanning) }
        if MockEngine.isEnabledAtLaunch {
            state.isMockEngine = true
            engine.setMockStatus()
            mock.start()  // FR-A11: fixtures instead of the engine
        } else {
            engine.start()
        }

        watcher = ConversationWatcher(reader: ConversationReader(rules: rules.conversation))
        watcher.onMessage = { [weak self] completed in
            // A brand-new chat is named like the engine and watcher name it: its first message, then
            // "first message + first reply" once the reply exists (ConversationWatcher.chatKey).
            if let self {
                if self.currentChatKey == nil, completed.message.position == 0 {
                    self.currentChatKey = completed.id
                } else if completed.message.position == 1, let first = self.currentChatKey,
                          !first.contains("+") {
                    self.renameChatMemory(from: first, to: first + "+" + completed.id)
                }
            }
            self?.messagePositions[completed.id] = completed.message.position
            self?.engine.sendMessage(completed)
            if completed.message.role == .assistant { self?.startScan(completed.id) }
        }
        watcher.onConversationChange = { [weak self] chatKey in
            self?.voice.stop()  // stop talking about the chat we just left
            self?.engine.startNewSession(chatKey: chatKey)
            self?.clearScans()
            self?.pet.resetForNewConversation()
            self?.lastVoice = nil
            self?.currentChatKey = chatKey
            self?.messagePositions = [:]
            self?.claimPositions = [:]
            self?.fixBoundary = nil
            self?.restoreChatMemory()
        }

        monitor = ClaudeAppMonitor(claudeBundleID: rules.claudeBundleID)
        monitor.onChange = { [weak self] app in
            self?.frontmostClaude = app
            self?.apply()
        }
        monitor.start()
        highlightScanner = HighlightScanner(rules: rules.conversation)
        startHighlighting()
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

    // MARK: - Per-chat memory

    private func remember(_ update: (inout ChatMemory) -> Void) {
        guard let key = currentChatKey else { return }
        var memory = chatMemory[key] ?? ChatMemory()
        update(&memory)
        chatMemory[key] = memory
        chatOrder.removeAll { $0 == key }
        chatOrder.append(key)
        if chatOrder.count > Self.rememberedChats {
            chatMemory.removeValue(forKey: chatOrder.removeFirst())
        }
    }

    /// The chat's key grew from "first message" to "first message + first reply": keep its memory.
    private func renameChatMemory(from old: String, to new: String) {
        if let memory = chatMemory.removeValue(forKey: old) { chatMemory[new] = memory }
        chatOrder = chatOrder.map { $0 == old ? new : $0 }
        currentChatKey = new
    }

    /// Back in a chat we've seen: its flagged claims (highlights, Details), bubble and last spoken
    /// line come back. The engine restores the heat itself.
    private func restoreChatMemory() {
        guard let key = currentChatKey, let memory = chatMemory[key] else { return }
        pet.restore(claims: memory.claims, bubble: memory.bubble, disagreed: memory.disagreed)
        claimPositions = memory.claimPositions
        fixBoundary = memory.fixBoundary
        lastVoice = memory.voice
        pet.model.hasVoiceLine = memory.voice != nil
        Log.pet.info("Restored chat: \(memory.claims.count) claims, voice line \(memory.voice != nil)")
    }

    // MARK: - On-screen highlights of flagged claims

    private func startHighlighting() {
        highlightTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshHighlights() }
        }
    }

    /// After a Fix it, nothing flagged so far is highlighted; errors that come back in later
    /// replies are flagged again by the engine and highlighted there.
    private func markFixBoundary() {
        let latest = max(messagePositions.values.max() ?? -1, claimPositions.values.max() ?? -1)
        guard latest >= 0 else { return }
        fixBoundary = latest
        remember { $0.fixBoundary = latest }
    }

    /// What to highlight: red/amber claims not disagreed with and not covered by a fix, each marked
    /// once, in the first reply it appeared in.
    private func highlightTargets() -> [HighlightTarget] {
        var targets: [String: HighlightTarget] = [:]
        for claim in pet.model.claims where !pet.model.disagreedClaimIDs.contains(claim.claimID) {
            let severity: HighlightTarget.Severity
            switch claim.final {
            case "red": severity = .red
            case "amber": severity = .amber
            default: continue
            }
            let position = claimPositions[claim.claimID]
            if let boundary = fixBoundary, (position ?? Int.min) <= boundary { continue }
            // Same wrong claim in several replies: keep the earliest one.
            let key = claim.quote.lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
            if let existing = targets[key], (existing.position ?? .max) <= (position ?? .max) { continue }
            targets[key] = HighlightTarget(quote: claim.quote, severity: severity, position: position)
        }
        return Array(targets.values)
    }

    /// Marks red/amber claims of the current chat on Claude's window, following scrolling.
    private func refreshHighlights() {
        guard state.isHighlighting, !state.isPaused, !state.isMockEngine,
              let pid = frontmostClaude?.processIdentifier
        else { highlightOverlay.hide(); return }
        let targets = highlightTargets()
        guard !targets.isEmpty else { highlightOverlay.hide(); return }
        guard !highlightBusy else { return }  // previous scan still running (long chat)
        highlightBusy = true
        let scanner = highlightScanner!
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = scanner.scan(pid: pid, targets: targets)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.highlightBusy = false
                    // Claude may have lost focus while we scanned.
                    guard self.frontmostClaude != nil, self.state.isHighlighting, let result else {
                        self.highlightOverlay.hide()
                        return
                    }
                    self.highlightOverlay.show(result.boxes, over: result.window, liveFrame: self.claudeWindowFrame)
                }
            }
        }
    }

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
                self.markFixBoundary()
                self.pet.closeBubbleAfterFix()
            case .copiedToClipboard:
                self.pet.showFixNote("Couldn't reach Claude's message box. The fix is on your clipboard: click the box and press ⌘V.")
                self.engine.sendCorrectionInserted(correctionID: correction.correctionID)
                self.markFixBoundary()
            }
        }
    }

    /// Session Brief: paste the context refresh into Claude's message box, the same way Fix it does.
    /// Never presses Enter (the user sends it), and it isn't a correction, so no correction.inserted.
    private func pasteBrief(_ offer: BriefOffer, mode: ComposerInserter.Mode?) {
        guard let app = frontmostClaude
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: rules.claudeBundleID).first
        else { return }

        var chosen = mode ?? .replace
        if mode == nil {
            switch inserter.state(pid: app.processIdentifier) {
            case .hasText:
                pet.askReplaceOrAddBrief(offer)
                return
            case .empty, .notFound:
                chosen = .replace
            }
        }

        inserter.insert(offer.text, into: app, mode: chosen) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .inserted:
                Log.pet.info("Context refresh pasted (\(offer.reason, privacy: .public))")
                self.pet.closeBubbleAfterBrief()
            case .copiedToClipboard:
                self.pet.showFixNote("Couldn't reach Claude's message box. The refresh is on your clipboard: click the box and press ⌘V.")
            }
        }
    }

    #if DEBUG
    /// Debug menu / demo: show a sample context-refresh offer without the engine.
    func previewBriefOffer() {
        pet.showBriefOffer(.sample)
    }
    #endif

    private func claudeVersion() -> String {
        let app = frontmostClaude
            ?? NSRunningApplication.runningApplications(withBundleIdentifier: rules.claudeBundleID).first
        return app?.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary?["CFBundleShortVersionString"] as? String }
            ?? "unknown"
    }

    /// FR-A11: switch between the real engine and fixture replay without restarting.
    func setMockEngine(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: "mockEngine")
        state.isMockEngine = on
        clearScans()
        pet.resetForNewConversation()
        if on {
            watcher.stop()
            engine.stop()
            engine.setMockStatus()
            mock.start()
        } else {
            mock.stop()
            engine.startNewSession(chatKey: nil)
            engine.start()
            apply()
        }
    }

    /// Menu › Character: Charlie or Marley. Remembered across launches; each has its own voice.
    func setCharacter(_ character: PetCharacter) {
        guard character != pet.model.character else { return }
        PetCharacter.saved = character
        state.character = character
        engine.setCharacter(character)  // her own voice and personality (engine)
        // 1) the current character sinks out of view, 2) swap while hidden, 3) the new one rises.
        withAnimation(.easeIn(duration: 0.25)) {
            pet.model.isCharacterHidden = true
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.pet.model.character = character
                withAnimation(.spring(response: 0.45, dampingFraction: 0.7)) {
                    self.pet.model.isCharacterHidden = false
                }
            }
        }
        Log.pet.info("Character: \(character.displayName, privacy: .public)")
    }

    /// Menu › Language: restarts the engine session for the same chat so it applies immediately.
    func setLanguage(_ language: PetLanguage) {
        guard language != state.language else { return }
        PetLanguage.saved = language
        state.language = language
        engine.setLanguage(language)
        Log.net.info("Language: \(language.displayName, privacy: .public)")
    }

    func stopVoice() {
        voice.stop()
    }

    /// Menu: reopen the FR-A10 onboarding window.
    func showOnboarding() {
        onboarding.show()
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
        if !state.isMockEngine {
            watcher.start(pid: app.processIdentifier)  // FR-A4 (mock mode doesn't read Claude)
        }
        pet.wantsVisible = true
    }
}
