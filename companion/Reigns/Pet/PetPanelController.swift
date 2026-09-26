import AppKit
import SwiftUI

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
    private let bubblePanel = BubblePanel()
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

        hostingView.onClick = { [weak self] in self?.toggleBubble() }
        hostingView.onDragEnded = { [weak self] center in
            self?.snapToNearestSide(from: center)
        }
    }

    /// Level, heat and bubble content (from the engine, or the debug preview).
    func update(level: Int, heat: Int, bubble: BubbleContent?) {
        model.level = level
        model.heat = heat
        model.bubble = bubble
        positionBubble(animated: true)
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
        positionBubble(animated: animated)
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
        if model.isBubbleOpen { setBubbleContent() }  // tail flips to the new side
        reposition(animated: true)
    }

    // MARK: - Speech bubble (FR-A8)

    private func toggleBubble() {
        model.isBubbleOpen ? closeBubble() : openBubble()
    }

    private func openBubble() {
        model.isBubbleOpen = true
        model.isDetailsOpen = false
        setBubbleContent()
        positionBubble(animated: false)
        bubblePanel.show()
    }

    private func closeBubble() {
        model.isBubbleOpen = false
        model.isDetailsOpen = false
        bubblePanel.hide()
    }

    private func setBubbleContent() {
        let actions = BubbleActions(
            fixIt: { correction in
                // FR-A9 (paste into Claude's message box) comes next.
                Log.pet.info("Fix it tapped (\(correction.promptType.rawValue, privacy: .public))")
            },
            disagree: { problem in
                // FR-A5 will send feedback.disagree for this claim.
                Log.pet.info("I disagree tapped for claim \(problem.claimID, privacy: .public)")
            },
            dismiss: { [weak self] in self?.closeBubble() })
        let tailEdge: HorizontalEdge = PetSide.saved == .right ? .trailing : .leading
        bubblePanel.setContent(BubbleView(model: model, actions: actions, tailEdge: tailEdge))
    }

    /// Bubble sits just above the visible part of the horse, aligned to the pet's outer edge so its
    /// tail points at the horse's centre.
    private func positionBubble(animated: Bool) {
        guard model.isBubbleOpen, let window = claudeWindow else { return }
        let pet = targetFrame(in: window)
        let x = PetSide.saved == .right ? pet.maxX - BubbleView.width : pet.minX
        let y = pet.minY + PetView.visibleHeight(level: model.level) + 4
        bubblePanel.place(bottomLeft: CGPoint(x: x, y: y), animated: animated)
    }

    private func setShown(_ show: Bool) {
        guard show != isShown else { return }
        isShown = show
        if show {
            panel.orderFrontRegardless()
        } else {
            closeBubble()
        }
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
