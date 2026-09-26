import AppKit
import SwiftUI

/// Hosts the bubble in its own non-activating panel above the pet. Grows upward: the bottom edge
/// (just above the horse's head) stays put while the content height changes.
@MainActor
final class BubblePanel {
    private let panel: PetPanel
    private let hostingView: FirstMouseHostingView<AnyView>
    private var bottomLeft = CGPoint.zero

    init() {
        panel = PetPanel(size: CGSize(width: BubbleView.width, height: 100))
        hostingView = FirstMouseHostingView(rootView: AnyView(EmptyView()))
        panel.contentView = hostingView
        hostingView.onSizeChange = { [weak self] in self?.fitToContent() }
    }

    var isVisible: Bool { panel.isVisible }

    func setContent(_ view: some View) {
        hostingView.rootView = AnyView(view)
        fitToContent()
    }

    /// Places the bubble's bottom-left corner (Cocoa coordinates).
    func place(bottomLeft: CGPoint, animated: Bool) {
        self.bottomLeft = bottomLeft
        let frame = NSRect(origin: bottomLeft, size: panel.frame.size)
        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.2
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: true)
        }
    }

    func show() {
        fitToContent()
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.15
            panel.animator().alphaValue = 1
        }
    }

    func hide() {
        panel.alphaValue = 0
        panel.orderOut(nil)
    }

    private func fitToContent() {
        let size = hostingView.fittingSize
        guard size.height > 0, size != panel.frame.size else { return }
        panel.setFrame(NSRect(origin: bottomLeft, size: size), display: true)
    }
}

/// NSHostingView that takes the first click (the panel never becomes key) and reports size changes
/// so the panel can resize to fit.
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    var onSizeChange: (() -> Void)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func invalidateIntrinsicContentSize() {
        super.invalidateIntrinsicContentSize()
        // Let SwiftUI finish its layout pass before measuring.
        DispatchQueue.main.async { [weak self] in self?.onSizeChange?() }
    }
}
