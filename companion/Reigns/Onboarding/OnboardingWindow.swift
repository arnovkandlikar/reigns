import AppKit
import ApplicationServices
import SwiftUI

/// FR-A10: shows the onboarding window until Accessibility permission is granted, then closes
/// itself and tells the app to start watching.
@MainActor
final class OnboardingWindow: NSObject, NSWindowDelegate {
    /// Called once permission is granted.
    var onTrusted: (() -> Void)?

    private static let settingsURL =
        URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!

    private var window: NSWindow?
    private var pollTimer: Timer?

    func show() {
        if window == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 440, height: 560),
                styleMask: [.titled, .closable, .fullSizeContentView],
                backing: .buffered, defer: false)
            window.title = "Welcome to Reigns"
            window.titlebarAppearsTransparent = true
            window.isReleasedWhenClosed = false
            window.delegate = self
            self.window = window
        }
        render(trusted: AXIsProcessTrusted())
        window?.center()
        // Reigns is a menu-bar app, so bring it forward for this window.
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        startPolling()
    }

    func close() {
        window?.close()
    }

    // MARK: - Private

    private func render(trusted: Bool) {
        window?.contentView = NSHostingView(rootView: OnboardingView(
            isTrusted: trusted,
            openSettings: { [weak self] in self?.openSettings() },
            later: { [weak self] in self?.close() }))
        window?.setContentSize(window?.contentView?.fittingSize ?? NSSize(width: 440, height: 560))
    }

    private func openSettings() {
        // Adds Reigns to the Accessibility list so the user only has to flip its switch.
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        NSWorkspace.shared.open(Self.settingsURL)
    }

    /// There's no notification for the permission changing, so check once a second.
    private func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkTrust() }
        }
    }

    private func checkTrust() {
        guard AXIsProcessTrusted() else { return }
        pollTimer?.invalidate()
        pollTimer = nil
        Log.app.info("Accessibility permission granted")
        render(trusted: true)
        onTrusted?()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            MainActor.assumeIsolated { self?.close() }
        }
    }

    func windowWillClose(_ notification: Notification) {
        pollTimer?.invalidate()
        pollTimer = nil
    }
}
