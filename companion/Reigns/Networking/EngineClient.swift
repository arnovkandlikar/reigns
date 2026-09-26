import Foundation

/// FR-A5: WebSocket client to the engine (§12). Sends session.start first on every connection,
/// reconnects with backoff 1 s → 2 s → 5 s, and queues outbound messages while offline.
///
/// The engine keeps one session per connection (FR-B1), so a new conversation means a new
/// session_id on a fresh connection.
@MainActor
final class EngineClient: NSObject {
    enum Status: String {
        case offline = "Offline"
        case connecting = "Connecting…"
        case connected = "Connected"
    }

    var onStatus: ((Status) -> Void)?
    var onHeat: ((HeatUpdate) -> Void)?
    var onBubble: ((BubbleContent) -> Void)?
    var onVerdicts: ((VerdictsUpdate) -> Void)?

    private static let backoff: [TimeInterval] = [1, 2, 5]
    private static let maxQueued = 200

    private let url: URL
    private let claudeVersion: () -> String
    private lazy var urlSession = URLSession(configuration: .default, delegate: self, delegateQueue: .main)

    private var task: URLSessionWebSocketTask?
    private var sessionID = EngineClient.newSessionID()
    private var status = Status.offline
    private var attempt = 0
    private var reconnectWork: DispatchWorkItem?
    private var queue: [Data] = []
    private var isRunning = false

    /// Engine URL: REIGNS_ENGINE_URL env var or the `engineURL` default, else ws://127.0.0.1:8765/ws.
    static func configuredURL() -> URL {
        let raw = ProcessInfo.processInfo.environment["REIGNS_ENGINE_URL"]
            ?? UserDefaults.standard.string(forKey: "engineURL")
            ?? "ws://127.0.0.1:8765/ws"
        return URL(string: raw) ?? URL(string: "ws://127.0.0.1:8765/ws")!
    }

    init(url: URL, claudeVersion: @escaping () -> String) {
        self.url = url
        self.claudeVersion = claudeVersion
    }

    func start() {
        guard !isRunning else { return }
        isRunning = true
        connect()
    }

    /// New conversation → new engine session on a fresh connection. Queued messages belong to the
    /// old conversation, so they're dropped.
    func startNewSession() {
        sessionID = Self.newSessionID()
        queue.removeAll()
        Log.net.info("New engine session \(self.sessionID, privacy: .public)")
        guard isRunning else { return }
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        attempt = 0
        connect()
    }

    // MARK: - Outbound (§12.1)

    func sendMessage(_ completed: CompletedMessage) {
        send("message.new", MessageNewPayload(
            messageID: completed.id, role: completed.message.role.rawValue,
            text: completed.message.text, position: completed.message.position))
    }

    func sendDisagree(claimID: String, note: String? = nil) {
        send("feedback.disagree", FeedbackDisagreePayload(claimID: claimID, note: note))
    }

    func sendCorrectionInserted(correctionID: String) {
        send("correction.inserted", CorrectionInsertedPayload(correctionID: correctionID))
    }

    private func send(_ type: String, _ payload: some Encodable) {
        guard let data = encode(type, payload) else { return }
        if status == .connected, let task {
            Log.net.info("Sent \(type, privacy: .public)")
            transmit(data, on: task)
        } else {
            queue.append(data)
            if queue.count > Self.maxQueued { queue.removeFirst(queue.count - Self.maxQueued) }
            Log.net.info("Engine offline; queued \(type, privacy: .public) (\(self.queue.count) waiting)")
        }
    }

    private func encode<P: Encodable>(_ type: String, _ payload: P) -> Data? {
        let envelope = Envelope(type: type, session_id: sessionID, ts: Self.timestamp(), payload: payload)
        do {
            return try JSONEncoder().encode(envelope)
        } catch {
            Log.net.error("Couldn't encode \(type, privacy: .public): \(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    private func transmit(_ data: Data, on task: URLSessionWebSocketTask) {
        let text = String(decoding: data, as: UTF8.self)
        task.send(.string(text)) { error in
            if let error {
                Log.net.error("Send failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    // MARK: - Connection

    private func connect() {
        reconnectWork?.cancel()
        setStatus(.connecting)
        let task = urlSession.webSocketTask(with: url)
        self.task = task
        task.resume()
        receive(on: task)
    }

    private func didOpen(_ task: URLSessionWebSocketTask) {
        guard task === self.task else { return }
        attempt = 0
        setStatus(.connected)
        Log.net.info("Engine connected; session \(self.sessionID, privacy: .public)")
        let bundle = Bundle.main.infoDictionary
        let companion = bundle?["CFBundleShortVersionString"] as? String ?? "1.0"
        if let hello = encode("session.start", SessionStartPayload(
            appVersion: claudeVersion(), companionVersion: companion)) {
            transmit(hello, on: task)
        }
        let pending = queue
        queue.removeAll()
        pending.forEach { transmit($0, on: task) }
        if !pending.isEmpty { Log.net.info("Flushed \(pending.count) queued messages") }
    }

    private func didClose(_ task: URLSessionWebSocketTask, reason: String) {
        guard task === self.task else { return }  // an old connection we replaced on purpose
        self.task = nil
        setStatus(.offline)
        guard isRunning else { return }
        let delay = Self.backoff[min(attempt, Self.backoff.count - 1)]
        attempt += 1
        Log.net.info("Engine disconnected (\(reason, privacy: .public)); retrying in \(Int(delay)) s")
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.connect() }
        }
        reconnectWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func setStatus(_ new: Status) {
        guard new != status else { return }
        status = new
        onStatus?(new)
    }

    // MARK: - Inbound (§12.2)

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    switch result {
                    case .success(let message):
                        self.handle(message)
                        self.receive(on: task)
                    case .failure(let error):
                        self.didClose(task, reason: error.localizedDescription)
                    }
                }
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let data: Data
        switch message {
        case .string(let text): data = Data(text.utf8)
        case .data(let raw): data = raw
        @unknown default: return
        }

        struct Header: Decodable { let type: String }
        struct Body<P: Decodable>: Decodable { let payload: P }
        let decoder = JSONDecoder()
        do {
            let type = try decoder.decode(Header.self, from: data).type
            switch type {
            case "heat.update":
                let heat = try decoder.decode(Body<HeatUpdate>.self, from: data).payload
                Log.net.info("heat.update heat=\(heat.heat) level=\(heat.level) recovered=\(heat.recovered)")
                onHeat?(heat)
            case "bubble.content":
                let bubble = try decoder.decode(Body<BubbleContent>.self, from: data).payload
                Log.net.info("bubble.content level=\(bubble.level) problems=\(bubble.problems.count)")
                onBubble?(bubble)
            case "verdicts.update":
                let verdicts = try decoder.decode(Body<VerdictsUpdate>.self, from: data).payload
                Log.net.info("verdicts.update \(verdicts.claims.count) claims")
                onVerdicts?(verdicts)
            case "error":
                let error = try decoder.decode(Body<EngineError>.self, from: data).payload
                Log.net.error("Engine error \(error.code, privacy: .public): \(error.message, privacy: .public)")
            case "voice.play":
                Log.net.info("voice.play received (playback not implemented yet)")
            default:
                Log.net.info("Ignoring unknown message type \(type, privacy: .public)")
            }
        } catch {
            // §14 rule 7: never crash on bad input; keep the pet in its current state.
            Log.net.error("Couldn't decode engine message: \(String(describing: error), privacy: .public)")
        }
    }

    // MARK: - Helpers

    private static func newSessionID() -> String {
        UUID().uuidString.lowercased()
    }

    /// ISO-8601 UTC, e.g. 2026-10-01T14:03:22Z (§12).
    private static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}

/// §12 envelope: {type, session_id, ts, payload}.
private struct Envelope<P: Encodable>: Encodable {
    let type: String
    let session_id: String
    let ts: String
    let payload: P
}

extension EngineClient: URLSessionWebSocketDelegate {
    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                                didOpenWithProtocol protocol: String?) {
        MainActor.assumeIsolated { didOpen(webSocketTask) }
    }

    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                                didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        MainActor.assumeIsolated { didClose(webSocketTask, reason: "closed \(closeCode.rawValue)") }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let webSocketTask = task as? URLSessionWebSocketTask else { return }
        MainActor.assumeIsolated {
            didClose(webSocketTask, reason: error?.localizedDescription ?? "completed")
        }
    }
}
