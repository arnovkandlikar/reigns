import AppKit
import ApplicationServices

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let state = AppState.shared
    private var rules = AXRules.fallback
    private var monitor: ClaudeAppMonitor!
    private var tracker: ClaudeWindowTracker!
    private let dockTracker = DockTracker()
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
        pet.model.level = level
        pet.model.heat = Self.previewHeat[level]
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
            pet.wantsVisible = false
            return
        }
        tracker.start(pid: app.processIdentifier)
        dockTracker.start()
        pet.wantsVisible = true
    }
}
