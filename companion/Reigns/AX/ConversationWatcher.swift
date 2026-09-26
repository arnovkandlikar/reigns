import CryptoKit
import Foundation

/// A message that has finished and should be sent to the engine as message.new (§12.1).
struct CompletedMessage {
    /// SHA-1 hex of "position:first 200 chars" (FR-A4).
    let id: String
    let message: ChatMessage
}

/// FR-A4: polls the conversation off the main thread and reports each message once it is complete
/// (text unchanged for ≥ 1.5 s, and Claude not still generating it). Never reports the same
/// message_id twice.
///
/// Conversations already on screen when first seen are treated as history and not reported; a
/// conversation that starts from an empty chat is reported from its first message.
final class ConversationWatcher: @unchecked Sendable {
    /// Called on the main thread, in conversation order.
    var onMessage: (@MainActor (CompletedMessage) -> Void)?
    /// Called on the main thread when the user moves to a different conversation (new engine session),
    /// with that chat's key (message_id of its first message) when it's on screen.
    var onConversationChange: (@MainActor (String?) -> Void)?

    private static let stableAfter: TimeInterval = 1.5
    private static let minPollInterval: TimeInterval = 0.5
    private static let maxPollInterval: TimeInterval = 5
    private static let idPrefixLength = 200
    /// Chats younger than this may still be getting their auto-generated title.
    private static let namingWindow = 4

    private let reader: ConversationReader
    private let queue = DispatchQueue(label: "app.reigns.conversation-watcher", qos: .userInitiated)

    // Everything below is confined to `queue`.
    private var pid: pid_t?
    private var generation = 0
    private var tracked: [Int: Tracked] = [:]
    private var sentIDs: Set<String> = []
    private var hasSeenConversation = false
    private var title: String?
    /// When the window started showing no messages (a new chat, or a blink while switching).
    private var emptySince: Date?
    private var sawEmptyChat = false

    private struct Tracked {
        var role: ChatMessage.Role
        var text: String
        var changedAt: Date
        var sentID: String?
        /// Seeded history, never actually sent to the engine.
        var isHistory = false
    }

    init(reader: ConversationReader) {
        self.reader = reader
    }

    func start(pid: pid_t) {
        queue.async { [self] in
            guard self.pid != pid else { return }
            if self.pid != nil { resetConversation() }  // Claude relaunched
            self.pid = pid
            generation += 1
            schedule(generation, after: 0)
        }
    }

    func stop() {
        queue.async { [self] in
            pid = nil
            generation += 1  // cancels the pending tick
        }
    }

    // MARK: - Polling

    private func schedule(_ generation: Int, after delay: TimeInterval) {
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.tick(generation) }
    }

    private func tick(_ generation: Int) {
        guard generation == self.generation, let pid else { return }
        let started = Date()
        let snapshot = reader.snapshot(pid: pid)
        let elapsed = Date().timeIntervalSince(started)
        process(snapshot, now: Date())
        // Long, non-virtualized conversations can take seconds to read; back off accordingly.
        let delay = min(max(Self.minPollInterval, elapsed * 2), Self.maxPollInterval)
        schedule(generation, after: delay)
    }

    // MARK: - Completion logic

    private func process(_ snapshot: ConversationSnapshot, now: Date) {
        let messages = snapshot.messages
        if snapshot.isIgnoredMode {
            emptySince = nil  // not an empty chat, just a tab we don't watch
            return
        }
        guard !messages.isEmpty else {
            // Switching conversations blanks the list for a split second; only an empty chat that
            // stays empty (someone about to type their first question) counts as a new chat.
            let since = emptySince ?? now
            emptySince = since
            if now.timeIntervalSince(since) >= Self.stableAfter { sawEmptyChat = true }
            return
        }
        emptySince = nil

        // A chat that sat empty is always a new conversation (or a slow-loading old one), even if
        // none of its positions overlap what we remember.
        if !hasSeenConversation || sawEmptyChat || titleChanged(to: snapshot.title, messages: messages)
            || isDifferentConversation(messages) {
            let wasSwitch = hasSeenConversation
            resetConversation()
            hasSeenConversation = true
            title = snapshot.title
            let chatKey = Self.chatKey(messages)
            // First chat seen after launch: also restart the session if we know which chat it is,
            // so its saved heat comes back.
            if wasSwitch || chatKey != nil { notifyConversationChange(chatKey: chatKey) }
            // A genuinely new chat stayed empty for a while and starts at message 0.
            let isNewChat = sawEmptyChat && messages.contains { $0.position == 0 }
            sawEmptyChat = false
            if !isNewChat {
                seedAsHistory(messages, now: now)
                Log.ax.info("Watching conversation; \(messages.count) existing messages treated as history")
                return
            }
            Log.ax.info("Watching a new conversation from its first message")
        }
        sawEmptyChat = false
        if let newTitle = snapshot.title { title = newTitle }

        let newestKnown = tracked.keys.max() ?? -1
        for message in messages {
            if var entry = tracked[message.position], entry.role == message.role {
                if entry.text != message.text {
                    if let sentID = entry.sentID, !entry.isHistory {
                        Log.ax.error("Message \(message.position) changed after it was sent as \(sentID, privacy: .public)")
                    }
                    entry.text = message.text
                    entry.changedAt = now
                    tracked[message.position] = entry
                }
            } else if message.position < newestKnown {
                // Older than what we've already seen: scrolled back into view, so it's history.
                let id = Self.messageID(position: message.position, text: message.text)
                tracked[message.position] = Tracked(
                    role: message.role, text: message.text, changedAt: now, sentID: id, isHistory: true)
                sentIDs.insert(id)
            } else {
                tracked[message.position] = Tracked(role: message.role, text: message.text, changedAt: now)
            }
        }

        let newest = messages.map(\.position).max()
        for message in messages.sorted(by: { $0.position < $1.position }) {
            guard var entry = tracked[message.position], entry.sentID == nil,
                  now.timeIntervalSince(entry.changedAt) >= Self.stableAfter,
                  !entry.text.isEmpty
            else { continue }
            // Backup signal: while Claude shows a Stop control, the newest reply may just be pausing.
            if snapshot.isResponding, message.position == newest, message.role == .assistant { continue }

            let id = Self.messageID(position: message.position, text: entry.text)
            entry.sentID = id
            tracked[message.position] = entry
            guard sentIDs.insert(id).inserted else { continue }

            let completed = CompletedMessage(id: id, message: ChatMessage(
                role: entry.role, text: entry.text, position: message.position))
            Log.ax.info(
                "Complete: [\(message.position)] \(message.role.rawValue, privacy: .public) \(entry.text.count) chars id=\(id.prefix(10), privacy: .public)")
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated { self?.onMessage?(completed) }
            }
        }
    }

    private func titleChanged(to newTitle: String?, messages: [ChatMessage]) -> Bool {
        // A missing title (page still loading) tells us nothing.
        guard hasSeenConversation, let newTitle, newTitle != title else { return false }
        // A young chat's title changes as it gets auto-named (and a new chat can briefly carry the
        // previous chat's title). Only call it a switch if its messages no longer match ours.
        let youngChat = (tracked.keys.max() ?? 0) < Self.namingWindow
        guard youngChat else { return true }
        let overlapping = messages.filter { tracked[$0.position] != nil }
        return overlapping.isEmpty || isDifferentConversation(messages)
    }

    /// Streaming only ever appends, and user messages never change, so an overlapping position whose
    /// role differs, or whose text isn't a prefix-extension of what we saw, means another conversation.
    private func isDifferentConversation(_ messages: [ChatMessage]) -> Bool {
        for message in messages {
            guard let known = tracked[message.position] else { continue }
            if known.role != message.role { return true }
            let length = min(known.text.count, message.text.count, 100)
            if known.text.prefix(length) != message.text.prefix(length) { return true }
        }
        return false
    }

    private func seedAsHistory(_ messages: [ChatMessage], now: Date) {
        for message in messages {
            let id = Self.messageID(position: message.position, text: message.text)
            tracked[message.position] = Tracked(
                role: message.role, text: message.text, changedAt: now, sentID: id, isHistory: true)
            sentIDs.insert(id)
        }
    }

    private func resetConversation() {
        tracked = [:]
        sentIDs = []
    }

    private func notifyConversationChange(chatKey: String?) {
        Log.ax.info("Conversation changed")
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.onConversationChange?(chatKey) }
        }
    }

    /// Stable id for a chat: the message_id its first message has (or had) when sent to the engine.
    static func chatKey(_ messages: [ChatMessage]) -> String? {
        guard let first = messages.first(where: { $0.position == 0 }) else { return nil }
        return messageID(position: 0, text: first.text)
    }

    static func messageID(position: Int, text: String) -> String {
        let key = "\(position):\(text.prefix(idPrefixLength))"
        return Insecure.SHA1.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
