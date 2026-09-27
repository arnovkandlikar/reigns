import ApplicationServices
import Foundation

/// One flagged claim to find on screen.
struct HighlightTarget: Equatable {
    enum Severity { case red, amber }
    let quote: String
    let severity: Severity
    /// Position of the Claude reply the claim was flagged in; only that reply is searched.
    /// nil = any Claude reply (first match only).
    let position: Int?
}

/// A box to draw, in AX (top-left origin, global) coordinates.
struct HighlightBox: Equatable {
    let rect: CGRect
    let severity: HighlightTarget.Severity
}

/// Finds where flagged claims appear in Claude's window: walks the visible static text of Claude's
/// replies (never the user's messages) and asks Chromium for the exact bounds of each quote
/// (AXBoundsForRange). Each claim is marked once: its first appearance in the reply it was flagged
/// in. Quotes that wrap are split into one box per line. Runs off the main thread.
struct HighlightScanner {
    let rules: AXRules.Conversation

    private static let maxNodes = 30_000

    /// Boxes to draw plus Claude's window frame (both AX coordinates).
    func scan(pid: pid_t, targets: [HighlightTarget]) -> (boxes: [HighlightBox], window: CGRect)? {
        guard !targets.isEmpty else { return nil }
        let app = AXUIElementCreateApplication(pid)
        guard let window = AX.element(app, kAXFocusedWindowAttribute) ?? AX.element(app, kAXMainWindowAttribute),
              let windowFrame = Self.frame(of: window)
        else { return nil }

        let texts = collectTexts(in: window)
        guard !texts.isEmpty else { return nil }  // Code tab or nothing on screen

        var boxes: [HighlightBox] = []
        for target in targets {
            // First appearance only, and only in the reply the claim came from.
            for text in texts where target.position == nil || text.position == target.position {
                guard let range = Self.locate(target.quote, in: text.value) else { continue }
                for rect in Self.lineRects(text.element, range: range) where rect.intersects(windowFrame) {
                    boxes.append(HighlightBox(rect: rect, severity: target.severity))
                }
                break
            }
        }
        return (boxes, windowFrame)
    }

    // MARK: - Finding text

    private struct TextRun {
        let element: AXUIElement
        let value: NSString
        /// Position of the Claude reply this text belongs to.
        let position: Int
    }

    private struct Pending {
        let element: AXUIElement
        let articleNumber: Int?
    }

    /// Visible static text of Claude's replies, in document order, tagged with the reply's position
    /// (from the "Message N" label, like ConversationReader). User messages are skipped entirely.
    /// Returns nothing in a mode we don't read (Code tab).
    private func collectTexts(in window: AXUIElement) -> [TextRun] {
        var result: [TextRun] = []
        var stack = [Pending(element: window, articleNumber: nil)]
        var visited = 0
        var headingIndex = 0
        var currentReply: Int?  // nil while inside a user message (or before any message)
        while let next = stack.popLast(), visited < Self.maxNodes {
            visited += 1
            let element = next.element
            let role = AX.string(element, kAXRoleAttribute) ?? ""
            if role == kAXRadioButtonRole,
               let label = AX.string(element, kAXTitleAttribute) ?? AX.string(element, kAXDescriptionAttribute),
               rules.ignoredModeTitles.contains(label) {
                var value: AnyObject?
                AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
                if (value as? Int) == 1 { return [] }
            }
            if rules.skipRoles.contains(role) { continue }
            let subrole = AX.string(element, kAXSubroleAttribute) ?? ""
            if rules.skipSubroles.contains(subrole) { continue }

            if role == kAXHeadingRole, let isAssistant = headingIsAssistant(element) {
                let position = next.articleNumber.map { $0 - 1 } ?? headingIndex
                currentReply = isAssistant ? position : nil
                headingIndex += 1
                continue  // the heading's own text is a screen-reader summary
            }
            if role == kAXStaticTextRole {
                if let position = currentReply, let value = AX.string(element, kAXValueAttribute), value.count >= 3 {
                    result.append(TextRun(element: element, value: value as NSString, position: position))
                }
                continue
            }

            var articleNumber = next.articleNumber
            if subrole == "AXDocumentArticle" {
                let label = AX.string(element, kAXTitleAttribute) ?? AX.string(element, kAXDescriptionAttribute) ?? ""
                if label.hasPrefix(rules.articleTitlePrefix) {
                    articleNumber = Int(label.dropFirst(rules.articleTitlePrefix.count).prefix(while: \.isNumber))
                }
            }
            stack.append(contentsOf: AX.children(element).reversed().map {
                Pending(element: $0, articleNumber: articleNumber)
            })
        }
        return result
    }

    /// true = "Claude responded: …", false = "You said: …", nil = not a message heading.
    private func headingIsAssistant(_ heading: AXUIElement) -> Bool? {
        let title = AX.string(heading, kAXTitleAttribute)
            ?? AX.children(heading).lazy.compactMap { AX.string($0, kAXValueAttribute) }.first
            ?? ""
        if title.hasPrefix(rules.assistantHeadingPrefix) { return true }
        if title.hasPrefix(rules.userHeadingPrefix) { return false }
        return nil
    }

    /// Where `quote` sits in one text run: the whole quote if it's there, otherwise the longest
    /// leading part (≥ 24 characters) when formatting split the claim across runs.
    private static func locate(_ quote: String, in text: NSString) -> NSRange? {
        let needle = quote.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard needle.count >= 3 else { return nil }
        let whole = text.range(of: needle, options: [.caseInsensitive])
        if whole.location != NSNotFound { return whole }
        guard needle.count > 24 else { return nil }
        var length = min(needle.count, 120)
        while length >= 24 {
            let prefix = String(needle.prefix(length))
            let hit = text.range(of: prefix, options: [.caseInsensitive])
            if hit.location != NSNotFound { return hit }
            length -= 12
        }
        return nil
    }

    // MARK: - Geometry

    /// One rect per visual line: ranges whose bounds are taller than a line are split in half.
    private static func lineRects(_ element: AXUIElement, range: NSRange, depth: Int = 0) -> [CGRect] {
        guard let rect = bounds(element, range), rect.width > 0, rect.height > 0 else { return [] }
        guard let lineHeight = bounds(element, NSRange(location: range.location, length: 1))?.height,
              lineHeight > 0, rect.height > lineHeight * 1.5, range.length > 1, depth < 6
        else { return [rect] }
        let half = range.length / 2
        return lineRects(element, range: NSRange(location: range.location, length: half), depth: depth + 1)
            + lineRects(element, range: NSRange(location: range.location + half, length: range.length - half),
                        depth: depth + 1)
    }

    private static func bounds(_ element: AXUIElement, _ range: NSRange) -> CGRect? {
        var cfRange = CFRange(location: range.location, length: range.length)
        guard let rangeValue = AXValueCreate(.cfRange, &cfRange) else { return nil }
        var result: AnyObject?
        guard AXUIElementCopyParameterizedAttributeValue(
            element, kAXBoundsForRangeParameterizedAttribute as CFString, rangeValue, &result) == .success,
            let result
        else { return nil }
        var rect = CGRect.zero
        AXValueGetValue(result as! AXValue, .cgRect, &rect)
        return rect
    }

    static func frame(of element: AXUIElement) -> CGRect? {
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
