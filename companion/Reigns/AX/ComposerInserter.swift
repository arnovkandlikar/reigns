import AppKit
import ApplicationServices

/// FR-A9: puts a correction prompt into Claude's message box via clipboard paste, restoring the
/// user's clipboard afterwards. Never presses Enter — the user always sends it themselves.
@MainActor
final class ComposerInserter {
    enum ComposerState {
        case notFound, empty, hasText
    }

    enum Mode {
        /// Replace whatever is in the message box.
        case replace
        /// Keep the user's text and add the prompt after it.
        case append
    }

    enum Outcome {
        case inserted
        /// Couldn't reach the message box; the prompt was left on the clipboard (PRD R2 fallback).
        case copiedToClipboard
    }

    private static let keyV: CGKeyCode = 9
    private static let keyA: CGKeyCode = 0
    private static let keyDown: CGKeyCode = 125
    private static let clipboardRestoreDelay: TimeInterval = 0.3  // FR-A9

    private let composerDOMClass: String

    init(composerDOMClass: String) {
        self.composerDOMClass = composerDOMClass
    }

    func state(pid: pid_t) -> ComposerState {
        guard let composer = findComposer(pid: pid) else { return .notFound }
        let text = AX.string(composer, kAXValueAttribute) ?? ""
        return text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .empty : .hasText
    }

    func insert(_ prompt: String, into app: NSRunningApplication, mode: Mode,
                completion: @escaping (Outcome) -> Void) {
        guard let composer = findComposer(pid: app.processIdentifier) else {
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            pasteboard.setString(prompt, forType: .string)
            Log.pet.error("Fix it: Claude's message box not found; prompt left on the clipboard")
            completion(.copiedToClipboard)
            return
        }

        app.activate()
        after(0.15) {
            // AX focus first; clicking the box is the fallback if the web view ignores it.
            if AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue) != .success {
                self.click(composer)
            }
            after(0.1) {
                switch mode {
                case .replace: Self.postKey(Self.keyA, flags: .maskCommand)    // select all → paste replaces
                case .append: Self.postKey(Self.keyDown, flags: .maskCommand)  // jump to the end
                }
                let text = mode == .append ? "\n\n" + prompt : prompt
                after(0.05) {
                    self.paste(text) { completion(.inserted) }
                }
            }
        }
    }

    // MARK: - Clipboard paste

    private func paste(_ text: String, then done: @escaping () -> Void) {
        let pasteboard = NSPasteboard.general
        let saved = Self.snapshot(pasteboard)
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        let ourChange = pasteboard.changeCount

        Self.postKey(Self.keyV, flags: .maskCommand)  // never Enter (PRD §3.2, §14 rule 6)

        after(Self.clipboardRestoreDelay) {
            // Don't clobber something the user copied in the meantime.
            if pasteboard.changeCount == ourChange {
                Self.restore(saved, to: pasteboard)
            }
            done()
        }
    }

    private static func snapshot(_ pasteboard: NSPasteboard) -> [[NSPasteboard.PasteboardType: Data]] {
        (pasteboard.pasteboardItems ?? []).map { item in
            Dictionary(uniqueKeysWithValues: item.types.compactMap { type in
                item.data(forType: type).map { (type, $0) }
            })
        }
    }

    private static func restore(_ items: [[NSPasteboard.PasteboardType: Data]], to pasteboard: NSPasteboard) {
        pasteboard.clearContents()
        guard !items.isEmpty else { return }
        pasteboard.writeObjects(items.map { entry in
            let item = NSPasteboardItem()
            for (type, data) in entry { item.setData(data, forType: type) }
            return item
        })
    }

    // MARK: - Finding the message box

    /// Claude's composer is an AXTextArea whose DOM classes include ProseMirror (Day-One Test).
    private func findComposer(pid: pid_t) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        guard let window = AX.element(app, kAXFocusedWindowAttribute) ?? AX.element(app, kAXMainWindowAttribute)
        else { return nil }
        var visited = 0
        return search(window, depth: 0, visited: &visited)
    }

    private func search(_ element: AXUIElement, depth: Int, visited: inout Int) -> AXUIElement? {
        guard visited < 20_000, depth < 150 else { return nil }
        visited += 1
        if AX.string(element, kAXRoleAttribute) == kAXTextAreaRole {
            var classes: AnyObject?
            if AXUIElementCopyAttributeValue(element, "AXDOMClassList" as CFString, &classes) == .success,
               let list = classes as? [String], list.contains(composerDOMClass) {
                return element
            }
        }
        for child in AX.children(element) {
            if let found = search(child, depth: depth + 1, visited: &visited) { return found }
        }
        return nil
    }

    // MARK: - Input events

    private func click(_ element: AXUIElement) {
        var posValue: AnyObject?
        var sizeValue: AnyObject?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posValue) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeValue) == .success
        else { return }
        var origin = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posValue as! AXValue, .cgPoint, &origin)
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        let point = CGPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)  // AX = CG coordinates
        let source = CGEventSource(stateID: .combinedSessionState)
        CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?
            .post(tap: .cghidEventTap)
        CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?
            .post(tap: .cghidEventTap)
    }

    private static func postKey(_ key: CGKeyCode, flags: CGEventFlags) {
        let source = CGEventSource(stateID: .combinedSessionState)
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false)
        else { return }
        down.flags = flags
        up.flags = flags
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

@MainActor
private func after(_ delay: TimeInterval, _ work: @escaping @MainActor () -> Void) {
    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
        MainActor.assumeIsolated { work() }
    }
}
