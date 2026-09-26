import AppKit
import SwiftUI

/// Hosts the pet and turns mouse input into either a click (FR-A8 bubble) or a drag that moves
/// the panel to another corner (FR-A2).
final class PetHostingView: NSHostingView<PetView> {
    var onClick: (() -> Void)?
    /// Called with the panel's center (screen coordinates) when a drag ends.
    var onDragEnded: ((CGPoint) -> Void)?

    private static let dragThreshold: CGFloat = 3
    private var mouseStart = NSPoint.zero
    private var originStart = NSPoint.zero
    private var isDragging = false

    required init(rootView: PetView) {
        super.init(rootView: rootView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    // The panel never becomes key, so accept the very first click.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        mouseStart = NSEvent.mouseLocation
        originStart = window?.frame.origin ?? .zero
        isDragging = false
    }

    override func mouseDragged(with event: NSEvent) {
        let now = NSEvent.mouseLocation
        let dx = now.x - mouseStart.x
        let dy = now.y - mouseStart.y
        if !isDragging, hypot(dx, dy) < Self.dragThreshold { return }
        isDragging = true
        window?.setFrameOrigin(NSPoint(x: originStart.x + dx, y: originStart.y + dy))
    }

    override func mouseUp(with event: NSEvent) {
        guard let frame = window?.frame else { return }
        if isDragging {
            onDragEnded?(CGPoint(x: frame.midX, y: frame.midY))
        } else {
            onClick?()
        }
        isDragging = false
    }
}
