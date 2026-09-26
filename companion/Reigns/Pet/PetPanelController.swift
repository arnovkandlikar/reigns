import AppKit
import SwiftUI

/// FR-A6: expression changes spring into place.
private let expressionSpring = Animation.spring(response: 0.45, dampingFraction: 0.65)

/// Owns the pet panel: FR-A1 show/hide fade and FR-A2 positioning. The panel's bottom edge is the
/// "floor" the horse peeks up from: Claude's window bottom, or the top of the Dock when the Dock
/// would cover the pet.
@MainActor
final class PetPanelController {
    /// FR-A1: hide within 300 ms.
    private static let fadeDuration: TimeInterval = 0.25

    let model = PetViewModel()
    /// FR-A8: I disagree on the top claim → feedback.disagree (sent by the app delegate).
    var onDisagree: ((String) -> Void)?
    /// FR-A9: Fix it (mode nil = not chosen yet; the app delegate checks the message box first).
    var onFixIt: ((Correction, ComposerInserter.Mode?) -> Void)?
    /// Bubble voice controls.
    var onReplayVoice: (() -> Void)?
    var onToggleMute: (() -> Void)?

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

    /// Level, heat and bubble content (debug preview).
    func update(level: Int, heat: Int, bubble: BubbleContent?, recovered: Bool = false, unverified: Int = 0) {
        model.unverifiedCount = unverified
        withAnimation(expressionSpring) {
            model.level = level
            model.heat = heat
            model.isRecovered = recovered
        }
        model.bubble = bubble
        model.hasUnseenIssue = level >= 1 && !model.isBubbleOpen
        positionBubble(animated: true)
    }

    /// FR-A6: the level is driven only by heat.update.level.
    func apply(_ heat: HeatUpdate) {
        withAnimation(expressionSpring) {
            model.level = min(max(heat.level, 0), 4)
            model.heat = heat.heat
            model.isRecovered = heat.recovered
        }
        model.unverifiedCount = heat.amberCount
        positionBubble(animated: true)
    }

    /// Thinking bubble on/off while the engine checks a reply; the speech bubble moves above it.
    func setScanning(_ scanning: Bool) {
        guard model.isScanning != scanning else { return }
        model.isScanning = scanning
        positionBubble(animated: true)
    }

    func apply(_ bubble: BubbleContent) {
        model.bubble = bubble
        // Something's wrong and the user hasn't looked yet: nudge them to click.
        if (bubble.level >= 1 || !bubble.problems.isEmpty) && !model.isBubbleOpen {
            model.hasUnseenIssue = true
        } else if bubble.level == 0 && bubble.problems.isEmpty {
            model.hasUnseenIssue = false
        }
    }

    /// Merges a reply's verdicts into the conversation's claim list (replacing re-checked claims).
    func apply(_ verdicts: VerdictsUpdate) {
        var claims = model.claims
        for claim in verdicts.claims {
            if let index = claims.firstIndex(where: { $0.claimID == claim.claimID }) {
                claims[index] = claim
            } else {
                claims.append(claim)
            }
        }
        model.claims = claims
    }

    /// New conversation: back to Calm with nothing to show until the engine reports.
    func resetForNewConversation() {
        withAnimation(expressionSpring) {
            model.level = 0
            model.heat = 0
            model.isRecovered = false
        }
        model.unverifiedCount = 0
        model.bubble = nil
        model.claims = []
        model.hasUnseenIssue = false
        model.hasVoiceLine = false
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

    /// FR-A9: ask inline whether to replace or add to the user's text.
    func askReplaceOrAdd(_ correction: Correction) {
        model.pendingFix = correction
    }

    /// FR-A9 R2 fallback: the prompt is on the clipboard for the user to paste.
    func showFixNote(_ note: String) {
        model.fixNote = note
    }

    func closeBubbleAfterFix() {
        closeBubble()
    }

    private func openBubble() {
        model.isBubbleOpen = true
        model.hasUnseenIssue = false
        model.isDetailsOpen = false
        model.pendingFix = nil
        model.fixNote = nil
        setBubbleContent()
        positionBubble(animated: false)
        bubblePanel.show()
    }

    private func closeBubble() {
        model.isBubbleOpen = false
        model.isDetailsOpen = false
        model.pendingFix = nil
        model.fixNote = nil
        bubblePanel.hide()
    }

    private func setBubbleContent() {
        let actions = BubbleActions(
            fixIt: { [weak self] correction in
                Log.pet.info("Fix it tapped (\(correction.promptType.rawValue, privacy: .public))")
                self?.onFixIt?(correction, nil)
            },
            fixChoice: { [weak self] correction, mode in
                self?.model.pendingFix = nil
                if let mode { self?.onFixIt?(correction, mode) }
            },
            disagree: { [weak self] problem in
                Log.pet.info("I disagree tapped for claim \(problem.claimID, privacy: .public)")
                self?.onDisagree?(problem.claimID)
            },
            dismiss: { [weak self] in self?.closeBubble() },
            replayVoice: { [weak self] in self?.onReplayVoice?() },
            toggleMute: { [weak self] in self?.onToggleMute?() })
        let tailEdge: HorizontalEdge = PetSide.saved == .right ? .trailing : .leading
        bubblePanel.setContent(BubbleView(model: model, actions: actions, tailEdge: tailEdge))
    }

    /// Bubble sits just above the visible part of the horse, aligned to the pet's outer edge so its
    /// tail points at the horse's centre.
    private func positionBubble(animated: Bool) {
        guard model.isBubbleOpen, let window = claudeWindow else { return }
        let pet = targetFrame(in: window)
        let x = PetSide.saved == .right ? pet.maxX - BubbleView.width : pet.minX
        let y = pet.minY + PetView.visibleHeight(level: model.level, recovered: model.isRecovered)
            + PetView.accessoryClearance(level: model.level, recovered: model.isRecovered,
                                         scanning: model.isScanning) + 4
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
