import AppKit
import ApplicationServices

/// FR-A2: tracks the frame of Claude's focused window by polling AX (PRD allows a 250 ms poll
/// instead of AXObserver). Frames are reported in Cocoa screen coordinates (bottom-left origin).
@MainActor
final class ClaudeWindowTracker {
    /// Called only when the frame changes; nil means no usable window (closed or minimized).
    var onFrameChange: ((CGRect?) -> Void)?

    private let pollInterval: TimeInterval
    private var appElement: AXUIElement?
    private var timer: Timer?
    private var lastFrame: CGRect?
    private var hasReported = false

    init(pollInterval: TimeInterval) {
        self.pollInterval = pollInterval
    }

    func start(pid: pid_t) {
        stop()
        appElement = AXUIElementCreateApplication(pid)
        hasReported = false
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        appElement = nil
    }

    private func tick() {
        let frame = currentWindowFrame()
        guard !hasReported || frame != lastFrame else { return }
        hasReported = true
        lastFrame = frame
        onFrameChange?(frame)
    }

    private func currentWindowFrame() -> CGRect? {
        guard let appElement,
              let window = element(appElement, kAXFocusedWindowAttribute)
                ?? element(appElement, kAXMainWindowAttribute)
        else { return nil }

        var minimized: AnyObject?
        if AXUIElementCopyAttributeValue(window, kAXMinimizedAttribute as CFString, &minimized) == .success,
           (minimized as? Bool) == true {
            return nil
        }

        var posValue: AnyObject?
        var sizeValue: AnyObject?
        guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString, &posValue) == .success,
              AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeValue) == .success
        else { return nil }

        var origin = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posValue as! AXValue, .cgPoint, &origin)
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        return Self.cocoaRect(fromAX: CGRect(origin: origin, size: size))
    }

    private func element(_ parent: AXUIElement, _ attribute: String) -> AXUIElement? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(parent, attribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXUIElementGetTypeID()
        else { return nil }
        return (value as! AXUIElement)
    }

    /// AX uses a top-left origin on the primary screen; AppKit uses bottom-left.
    static func cocoaRect(fromAX rect: CGRect) -> CGRect {
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return CGRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }
}
