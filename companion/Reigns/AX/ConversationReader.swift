import ApplicationServices
import Foundation

/// One message in the Claude conversation, in on-screen order.
struct ChatMessage: Equatable {
    enum Role: String {
        case user, assistant
    }

    let role: Role
    let text: String
    /// 0-based index in the conversation (§12.1 message.new.position).
    let position: Int
}

/// One read of the conversation.
struct ConversationSnapshot {
    var messages: [ChatMessage]
    /// The conversation's title from the page ("<title> - Claude"); generic ("Claude") in the Code tab
    /// and before a new chat is named.
    var title: String?
    /// Claude is still generating (a "Stop" control is showing), so the newest reply isn't final.
    var isResponding: Bool
    /// The window is in a mode we don't watch (the Code tab): messages are left empty.
    var isIgnoredMode = false
}

/// FR-A3: reads the Claude conversation out of the AX tree.
///
/// Rule (documented in companion/README.md, strings in AXRules.json): every message begins with a
/// screen-reader-only AXHeading titled "You said: …" or "Claude responded: …". A message's text is
/// all AXStaticText after its heading and before the next one, inside the smallest element that
/// contains every heading (which keeps the composer, sidebar and footer out).
struct ConversationReader {
    let rules: AXRules.Conversation

    private static let maxNodes = 40_000
    private static let maxDepth = 150

    /// Enables Chromium's accessibility tree for Claude (Electron builds it lazily).
    static func enableAccessibility(pid: pid_t) {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    }

    /// Returns the messages in Claude's focused window, oldest first. Empty if none are found.
    func read(pid: pid_t) -> [ChatMessage] {
        snapshot(pid: pid).messages
    }

    func snapshot(pid: pid_t) -> ConversationSnapshot {
        let app = AXUIElementCreateApplication(pid)
        guard let window = AX.element(app, kAXFocusedWindowAttribute) ?? AX.element(app, kAXMainWindowAttribute)
        else { return ConversationSnapshot(messages: [], title: nil, isResponding: false) }

        var walker = Walker(rules: rules)
        walker.walk(window, path: [], block: 0, article: nil, inCode: false)
        if walker.inIgnoredMode {
            // Code sessions are full of private project facts no detector can verify, and the PRD
            // scopes Witness to Claude conversations, so we don't read them at all.
            return ConversationSnapshot(messages: [], title: walker.pageTitle, isResponding: false, isIgnoredMode: true)
        }
        return ConversationSnapshot(
            messages: Self.segment(walker.items, rules: rules), title: walker.pageTitle,
            isResponding: walker.sawStopControl)
    }

    // MARK: - Tree walk

    fileprivate enum Item {
        case heading(ChatMessage.Role, article: Article?, path: [Int])
        /// `inCode`: inside a code span/block, where text runs are highlighter tokens that already
        /// carry their own line breaks and must be joined exactly as-is.
        case text(String, block: Int, path: [Int], inCode: Bool)
    }

    /// The per-message AXDocumentArticle a heading sits in.
    fileprivate struct Article {
        let number: Int?
        let path: [Int]
    }

    private struct Walker {
        let rules: AXRules.Conversation
        var items: [Item] = []
        var sawStopControl = false
        var inIgnoredMode = false
        var pageTitle: String?
        var nodeCount = 0
        var nextBlock = 1

        mutating func walk(_ element: AXUIElement, path: [Int], block: Int, article: Article?, inCode: Bool) {
            guard nodeCount < ConversationReader.maxNodes, path.count < ConversationReader.maxDepth else { return }
            nodeCount += 1

            let role = AX.string(element, kAXRoleAttribute) ?? ""
            if role == kAXButtonRole, !sawStopControl,
               let label = AX.string(element, kAXTitleAttribute) ?? AX.string(element, kAXDescriptionAttribute),
               rules.respondingButtonTitles.contains(label) {
                sawStopControl = true
            }
            if role == kAXRadioButtonRole, !inIgnoredMode,
               let label = AX.string(element, kAXTitleAttribute) ?? AX.string(element, kAXDescriptionAttribute),
               rules.ignoredModeTitles.contains(label) {
                var value: AnyObject?
                AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
                if (value as? Int) == 1 { inIgnoredMode = true }
            }
            if role == "AXWebArea", pageTitle == nil, let title = AX.string(element, kAXTitleAttribute) {
                pageTitle = title
            }
            if rules.skipRoles.contains(role) { return }
            let subrole = AX.string(element, kAXSubroleAttribute) ?? ""
            if rules.skipSubroles.contains(subrole) || isSkippedByDOM(element, role: role) { return }

            if role == kAXHeadingRole, let messageRole = headingRole(element) {
                items.append(.heading(messageRole, article: article, path: path))
                return  // the heading's own text is a screen-reader summary, not message content
            }
            if role == kAXStaticTextRole {
                if let value = AX.string(element, kAXValueAttribute) {
                    items.append(.text(value, block: block, path: path, inCode: inCode))
                }
                return
            }

            var article = article
            if subrole == "AXDocumentArticle" {
                let label = AX.string(element, kAXTitleAttribute) ?? AX.string(element, kAXDescriptionAttribute) ?? ""
                // "Message 12" (Code tab) or "Message 12 of 40" (Chat tab).
                let number = label.hasPrefix(rules.articleTitlePrefix)
                    ? Int(label.dropFirst(rules.articleTitlePrefix.count).prefix(while: \.isNumber)) : nil
                article = Article(number: number, path: path)
            }

            // Inline wrappers (bold, code, links) continue the current line; anything else starts
            // a new block so paragraphs and list items come out on separate lines.
            let isInline = subrole.hasSuffix("StyleGroup") || role == "AXLink"
            let childBlock: Int
            if isInline {
                childBlock = block
            } else {
                childBlock = nextBlock
                nextBlock += 1
            }

            for (index, child) in AX.children(element).enumerated() {
                walk(child, path: path + [index], block: childBlock, article: article,
                     inCode: inCode || subrole == "AXCodeStyleGroup")
            }
        }

        /// App widgets inside replies (visuals, tool summaries, file cards) matched by DOM id/class.
        private func isSkippedByDOM(_ element: AXUIElement, role: String) -> Bool {
            guard role != kAXStaticTextRole else { return false }
            if let id = AX.string(element, "AXDOMIdentifier"),
               rules.skipDOMIDPrefixes.contains(where: id.hasPrefix)
                || rules.skipDOMIDSuffixes.contains(where: id.hasSuffix) {
                return true
            }
            guard !rules.skipDOMClasses.isEmpty else { return false }
            var classes: AnyObject?
            guard AXUIElementCopyAttributeValue(element, "AXDOMClassList" as CFString, &classes) == .success,
                  let list = classes as? [String]
            else { return false }
            return list.contains(where: rules.skipDOMClasses.contains)
        }

        private func headingRole(_ heading: AXUIElement) -> ChatMessage.Role? {
            let title = AX.string(heading, kAXTitleAttribute)
                ?? AX.children(heading).lazy.compactMap { AX.string($0, kAXValueAttribute) }.first
                ?? ""
            if title.hasPrefix(rules.userHeadingPrefix) { return .user }
            if title.hasPrefix(rules.assistantHeadingPrefix) { return .assistant }
            return nil
        }
    }

    // MARK: - Segmentation

    fileprivate static func segment(_ items: [Item], rules: AXRules.Conversation) -> [ChatMessage] {
        let headingPaths = items.compactMap { item -> [Int]? in
            if case let .heading(_, _, path) = item { return path }
            return nil
        }
        guard !headingPaths.isEmpty else { return [] }

        // With one heading there's nothing to intersect, so climb a fixed number of levels instead.
        let container = headingPaths.count > 1
            ? commonPrefix(headingPaths)
            : Array(headingPaths[0].dropLast(rules.singleMessageContainerLevelsUp))

        var messages: [ChatMessage] = []
        var role: ChatMessage.Role?
        var scope = container
        var position = 0
        var text = ""
        var lastBlock: Int?
        var lastTextPath: [Int]?
        var lastInCode = false

        func flush() {
            guard let role else { return }
            messages.append(ChatMessage(
                role: role, text: text.trimmingCharacters(in: .whitespacesAndNewlines), position: position))
        }

        for item in items {
            switch item {
            case let .heading(newRole, article, path):
                guard path.starts(with: container) else { continue }
                flush()
                // "Message N" is 1-based and survives list virtualization; fall back to counting.
                position = article?.number.map { $0 - 1 } ?? (messages.last.map { $0.position + 1 } ?? 0)
                role = newRole
                // A user message is only its own article. An assistant reply can continue in sibling
                // rows, so it runs to the next heading. Text after a user message with no heading of
                // its own is a reply still streaming, which FR-A4 must not send yet.
                scope = newRole == .user ? (article?.path ?? container) : container
                text = ""
                lastBlock = nil
                lastTextPath = nil
            case let .text(value, block, path, inCode):
                guard role != nil, path.starts(with: scope),
                      !rules.ignoredTexts.contains(where: value.hasPrefix)
                else { continue }
                // New line when the block changes, or when two text runs are direct neighbours:
                // Chromium splits multi-line text at line breaks, while inline markup (bold, code,
                // links) always sits between runs in its own group.
                let codeContinues = inCode && lastInCode && lastBlock == block
                let newLine = (lastBlock.map { $0 != block } ?? false)
                    || (!codeContinues && isNextSibling(path, of: lastTextPath))
                if newLine, !text.hasSuffix("\n") { text += "\n" }
                text += value
                lastBlock = block
                lastTextPath = path
                lastInCode = inCode
            }
        }
        flush()
        return messages
    }

    private static func isNextSibling(_ path: [Int], of previous: [Int]?) -> Bool {
        guard let previous, previous.count == path.count, let last = path.last, let prevLast = previous.last
        else { return false }
        return path.dropLast() == previous.dropLast() && last == prevLast + 1
    }

    private static func commonPrefix(_ paths: [[Int]]) -> [Int] {
        var prefix = paths[0]
        for path in paths.dropFirst() {
            let shared = zip(prefix, path).prefix { $0 == $1 }.count
            prefix = Array(prefix.prefix(shared))
        }
        return prefix
    }
}

/// Small AX attribute helpers shared by the AX readers.
enum AX {
    static func string(_ element: AXUIElement, _ attribute: String) -> String? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let string = value as? String, !string.isEmpty
        else { return nil }
        return string
    }

    static func element(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXUIElementGetTypeID()
        else { return nil }
        return (value as! AXUIElement)
    }

    static func children(_ element: AXUIElement) -> [AXUIElement] {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success
        else { return [] }
        return (value as? [AXUIElement]) ?? []
    }
}
