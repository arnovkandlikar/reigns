import AppKit
import ApplicationServices

/// Reports the Dock's on-screen frame so the pet can peek above it instead of being covered.
/// There's no notification for an auto-hiding Dock sliding in, so we poll its AX list frame:
/// when hidden it sits just below the screen edge.
@MainActor
final class DockTracker {
    /// Cocoa-coordinate frame of the visible Dock, or nil when it's hidden.
    var onChange: ((CGRect?) -> Void)?

    private static let pollInterval: TimeInterval = 0.1
    private var timer: Timer?
    private var dockList: AXUIElement?
    private var lastFrame: CGRect?

    func start() {
        guard timer == nil else { return }
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        let frame = visibleDockFrame()
        guard frame != lastFrame else { return }
        lastFrame = frame
        onChange?(frame)
    }

    private func visibleDockFrame() -> CGRect? {
        guard let list = dockList ?? findDockList(),
              let axRect = Self.frame(of: list)
        else {
            dockList = nil  // Dock restarted or not found; look it up again next tick
            return nil
        }
        let frame = ClaudeWindowTracker.cocoaRect(fromAX: axRect)
        let screen = NSScreen.screens.first?.frame ?? .zero
        // Hidden Dock: sits entirely outside the screen (a couple of px of slack for the edge).
        return frame.intersection(screen).height > 2 ? frame : nil
    }

    private func findDockList() -> AXUIElement? {
        guard let dock = NSRunningApplication.runningApplications(
            withBundleIdentifier: "com.apple.dock").first
        else { return nil }
        let app = AXUIElementCreateApplication(dock.processIdentifier)
        var children: AnyObject?
        guard AXUIElementCopyAttributeValue(app, kAXChildrenAttribute as CFString, &children) == .success
        else { return nil }
        dockList = (children as? [AXUIElement])?.first { element in
            var role: AnyObject?
            AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
            return (role as? String) == kAXListRole
        }
        return dockList
    }

    private static func frame(of element: AXUIElement) -> CGRect? {
        var posValue: AnyObject?
        var sizeValue: AnyObject?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posValue) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeValue) == .success
        else { return nil }
        var origin = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posValue as! AXValue, .cgPoint, &origin)
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        return CGRect(origin: origin, size: size)
    }
}
