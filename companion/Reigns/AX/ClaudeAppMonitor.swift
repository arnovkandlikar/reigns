import AppKit

/// FR-A1: reports whether the Claude app is frontmost, via NSWorkspace activation notifications.
@MainActor
final class ClaudeAppMonitor {
    /// Called with Claude's NSRunningApplication when it becomes frontmost, nil when another app does.
    var onChange: ((NSRunningApplication?) -> Void)?

    private let claudeBundleID: String
    private var observer: NSObjectProtocol?

    init(claudeBundleID: String) {
        self.claudeBundleID = claudeBundleID
    }

    func start() {
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            MainActor.assumeIsolated { self?.handleActivation(app) }
        }
        handleActivation(NSWorkspace.shared.frontmostApplication)
    }

    private func handleActivation(_ app: NSRunningApplication?) {
        // Clicking our own menu bar item activates Reigns; don't treat that as leaving Claude.
        if app?.bundleIdentifier == Bundle.main.bundleIdentifier { return }
        let isClaude = app?.bundleIdentifier == claudeBundleID
        Log.ax.debug("Frontmost: \(app?.bundleIdentifier ?? "nil", privacy: .public) claude=\(isClaude)")
        onChange?(isClaude ? app : nil)
    }

    deinit {
        if let observer { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
    }
}
