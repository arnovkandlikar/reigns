import AppKit
import SwiftUI

/// Click-through window over Claude's window that marks hallucinated text: red = likely wrong,
/// orange = couldn't confirm. Sits above Claude and below the pet.
@MainActor
final class HighlightOverlay {
    private let panel: NSPanel
    private let model = HighlightModel()
    /// Size of Claude's window when the boxes were measured (they're relative to its top-left).
    private var measuredSize: CGSize?

    init() {
        panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.floating.rawValue - 1)
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.ignoresMouseEvents = true  // never gets in the way of reading or clicking
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.contentView = NSHostingView(rootView: HighlightCanvas(model: model))
    }

    /// `boxes` and `claudeWindowAX` are in AX coordinates (top-left origin). `liveFrame` is the
    /// window's latest Cocoa frame from the tracker: if the window moved while we were scanning, the
    /// boxes (relative to the window) are placed on where it is now, not where it was.
    func show(_ boxes: [HighlightBox], over claudeWindowAX: CGRect, liveFrame: CGRect?) {
        guard !boxes.isEmpty else { hide(); return }
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        let scannedFrame = CGRect(x: claudeWindowAX.minX, y: primaryHeight - claudeWindowAX.maxY,
                                  width: claudeWindowAX.width, height: claudeWindowAX.height)
        let cocoaFrame = liveFrame.flatMap { $0.size == scannedFrame.size ? $0 : nil } ?? scannedFrame
        measuredSize = claudeWindowAX.size
        if panel.frame != cocoaFrame { panel.setFrame(cocoaFrame, display: false) }
        // Window-local, top-left coordinates for SwiftUI.
        model.boxes = boxes.map { box in
            HighlightBox(rect: box.rect.offsetBy(dx: -claudeWindowAX.minX, dy: -claudeWindowAX.minY),
                         severity: box.severity)
        }
        if !panel.isVisible { panel.orderFrontRegardless() }
    }

    /// Claude's window moved: move the marks with it on the same frame (no re-scan needed, they're
    /// relative to the window). On a resize the text reflows, so hide until the next scan.
    func follow(_ claudeWindow: CGRect?) {
        guard panel.isVisible else { return }
        guard let claudeWindow, claudeWindow.size == measuredSize else {
            hide()
            return
        }
        if panel.frame != claudeWindow { panel.setFrame(claudeWindow, display: false) }
    }

    func hide() {
        model.boxes = []
        measuredSize = nil
        panel.orderOut(nil)
    }
}

@MainActor
@Observable
private final class HighlightModel {
    var boxes: [HighlightBox] = []
}

private struct HighlightCanvas: View {
    let model: HighlightModel

    var body: some View {
        Canvas { context, _ in
            for box in model.boxes {
                let color: Color = box.severity == .red ? .red : .orange
                let rect = box.rect.insetBy(dx: -2, dy: -1)
                context.fill(Path(roundedRect: rect, cornerRadius: 3), with: .color(color.opacity(0.18)))
                // Underline: solid for likely wrong, dashed for couldn't confirm.
                var underline = Path()
                underline.move(to: CGPoint(x: rect.minX, y: rect.maxY))
                underline.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
                context.stroke(underline, with: .color(color.opacity(0.9)),
                               style: StrokeStyle(lineWidth: 2, lineCap: .round,
                                                  dash: box.severity == .red ? [] : [4, 3]))
            }
        }
        .allowsHitTesting(false)
    }
}
