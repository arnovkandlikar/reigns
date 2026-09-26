import AppKit

/// Owns the pet panel: FR-A1 show/hide fade and FR-A2 positioning. The panel's bottom edge is the
/// "floor" the horse peeks up from: Claude's window bottom, or the top of the Dock when the Dock
/// would cover the pet.
@MainActor
final class PetPanelController {
    /// FR-A1: hide within 300 ms.
    private static let fadeDuration: TimeInterval = 0.25

    let model = PetViewModel()

    /// Set by the app delegate: true while Claude is frontmost and Reigns isn't paused.
    var wantsVisible = false {
        didSet { refresh() }
    }

    private let inset: CGFloat
    private let panel: PetPanel
    private let hostingView: PetHostingView
    private var claudeWindow: CGRect?
    private var dockFrame: CGRect?
    private var isShown = false

    private var size: CGSize { PetView.panelSize }

    init(inset: Double) {
        self.inset = inset
        panel = PetPanel(size: PetView.panelSize)
        hostingView = PetHostingView(rootView: PetView(model: model))
        hostingView.frame = NSRect(origin: .zero, size: PetView.panelSize)
        panel.contentView = hostingView

        hostingView.onClick = {
            Log.pet.info("Pet clicked")  // FR-A8: speech bubble comes later
        }
        hostingView.onDragEnded = { [weak self] center in
            self?.snapToNearestSide(from: center)
        }
    }

    func claudeWindowChanged(_ frame: CGRect?) {
        claudeWindow = frame
        refresh()
    }

    func dockChanged(_ frame: CGRect?) {
        dockFrame = frame
        guard isShown else { return }
        reposition(animated: true)
    }

    private func refresh() {
        let show = wantsVisible && claudeWindow != nil
        if show { reposition(animated: false) }
        setShown(show)
    }

    private func targetFrame(in window: CGRect) -> NSRect {
        let x = PetSide.saved.x(for: size.width, in: window, inset: inset)
        return NSRect(origin: CGPoint(x: x, y: floorY(in: window, petX: x)), size: size)
    }

    /// Where the horse's floor is: the window's bottom (never below the screen), lifted above the
    /// Dock when the Dock overlaps the pet horizontally.
    private func floorY(in window: CGRect, petX: CGFloat) -> CGFloat {
        let screen = NSScreen.screens.first { $0.frame.intersects(window) } ?? NSScreen.main
        var floor = max(window.minY, screen?.frame.minY ?? window.minY)
        if let dock = dockFrame,
           dock.minX < petX + size.width, dock.maxX > petX,  // overlaps the pet horizontally
           dock.maxY > floor {
            floor = dock.maxY
        }
        return floor
    }

    private func reposition(animated: Bool) {
        guard let window = claudeWindow else { return }
        let frame = targetFrame(in: window)
        guard frame != panel.frame else { return }
        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.2
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: true)
        }
    }

    private func snapToNearestSide(from center: CGPoint) {
        guard let window = claudeWindow else { return }
        PetSide.saved = PetSide.nearest(toX: center.x, in: window)
        Log.pet.info("Pet moved to \(PetSide.saved.rawValue, privacy: .public) side")
        reposition(animated: true)
    }

    private func setShown(_ show: Bool) {
        guard show != isShown else { return }
        isShown = show
        if show { panel.orderFrontRegardless() }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = Self.fadeDuration
            panel.animator().alphaValue = show ? 1 : 0
        } completionHandler: { [weak self] in
            MainActor.assumeIsolated {
                // A later show may have started during the fade; only order out if still hidden.
                guard let self, !self.isShown else { return }
                self.panel.orderOut(nil)
            }
        }
    }
}
