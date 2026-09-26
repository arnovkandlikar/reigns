import AppKit
import ApplicationServices

/// FR-A2: tracks the frame of Claude's focused window. Frames are reported in Cocoa screen
/// coordinates (bottom-left origin).
///
/// To follow a window being dragged smoothly, it listens for AX moved/resized notifications and,
/// while the frame is changing, re-reads it once per screen refresh via the display link (120 Hz on
/// ProMotion, 60 Hz elsewhere) so the pet moves in step with each redraw. Once the window has been
/// still for half a second it drops back to a slow poll.
@MainActor
final class ClaudeWindowTracker: NSObject {
    /// Called only when the frame changes; nil means no usable window (closed or minimized).
    var onFrameChange: ((CGRect?) -> Void)?

    /// Seconds without change before leaving display-rate tracking.
    private static let settleTime: TimeInterval = 0.5

    private let slowInterval: TimeInterval
    private var appElement: AXUIElement?
    private var timer: Timer?
    private var displayLink: CADisplayLink?
    private var isFast = false
    private var lastChange = Date()
    private var lastFrame: CGRect?
    private var hasReported = false
    private var observer: AXObserver?

    init(pollInterval: TimeInterval) {
        self.slowInterval = pollInterval
    }

    func start(pid: pid_t) {
        stop()
        appElement = AXUIElementCreateApplication(pid)
        hasReported = false
        observe(pid: pid)
        tick()
        schedule(fast: false)
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        displayLink?.invalidate()
        displayLink = nil
        appElement = nil
        if let observer {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        }
        observer = nil
    }

    // MARK: - Polling

    private func schedule(fast: Bool) {
        timer?.invalidate()
        timer = nil
        displayLink?.invalidate()
        displayLink = nil
        isFast = fast
        lastChange = Date()
        if fast, let screen = NSScreen.main {
            // One read per screen refresh, right before the redraw.
            let link = screen.displayLink(target: self, selector: #selector(displayFrame))
            link.add(to: .main, forMode: .common)
            displayLink = link
        } else {
            let timer = Timer(timeInterval: slowInterval, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.tick() }
            }
            // .common so it keeps firing while menus or drags are tracking events.
            RunLoop.main.add(timer, forMode: .common)
            self.timer = timer
        }
    }

    @objc private func displayFrame() {
        tick()
    }

    private func tick() {
        let frame = currentWindowFrame()
        if hasReported && frame == lastFrame {
            if isFast, Date().timeIntervalSince(lastChange) >= Self.settleTime { schedule(fast: false) }
            return
        }
        hasReported = true
        lastFrame = frame
        onFrameChange?(frame)
        if isFast {
            lastChange = Date()
        } else {
            schedule(fast: true)  // it started moving: follow at display rate
        }
    }

    /// AX moved/resized notification: switch to fast polling right away.
    fileprivate func windowDidMove() {
        tick()
        if !isFast { schedule(fast: true) }
    }

    // MARK: - AX notifications

    private func observe(pid: pid_t) {
        var created: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon else { return }
            let tracker = Unmanaged<ClaudeWindowTracker>.fromOpaque(refcon).takeUnretainedValue()
            MainActor.assumeIsolated { tracker.windowDidMove() }
        }
        guard AXObserverCreate(pid, callback, &created) == .success, let created, let appElement else { return }
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for notification in [kAXWindowMovedNotification, kAXWindowResizedNotification] {
            AXObserverAddNotification(created, appElement, notification as CFString, refcon)
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(created), .commonModes)
        observer = created
    }

    // MARK: - Frame

    private func currentWindowFrame() -> CGRect? {
        guard let appElement,
              let window = AX.element(appElement, kAXFocusedWindowAttribute)
                ?? AX.element(appElement, kAXMainWindowAttribute)
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

    /// AX uses a top-left origin on the primary screen; AppKit uses bottom-left.
    static func cocoaRect(fromAX rect: CGRect) -> CGRect {
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return CGRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }
}
