import Foundation

/// FR-A11: replays shared/fixtures/scenarios/*.json instead of talking to the engine, so the pet and
/// bubble can be built and shown without it. Each scenario's recorded engine → companion messages
/// go through EngineClient's normal decoding path, so the pet behaves exactly as it would live.
@MainActor
final class MockEngine {
    /// New scenario = new chat (the pet resets like on a conversation switch).
    var onNewChat: ((String) -> Void)?
    /// Thinking bubble on/off around each assistant reply.
    var onScanning: ((Bool) -> Void)?

    private static let scanTime: TimeInterval = 2.5
    private static let messageGap: TimeInterval = 0.4
    private static let holdAfterScenario: TimeInterval = 7

    private let engine: EngineClient
    private var scenarios: [Scenario] = []
    private var generation = 0

    private struct Scenario {
        let name: String
        let description: String
        /// Engine replies grouped per step: [session.start reply, reply 1 (verdicts, heat, bubble), …]
        let steps: [[Data]]
    }

    init(engine: EngineClient) {
        self.engine = engine
    }

    /// REIGNS_MOCK=1 (or the PRD's WITNESS_MOCK=1), or the debug menu toggle.
    static var isEnabledAtLaunch: Bool {
        let env = ProcessInfo.processInfo.environment
        return env["REIGNS_MOCK"] == "1" || env["WITNESS_MOCK"] == "1"
            || UserDefaults.standard.bool(forKey: "mockEngine")
    }

    func start() {
        if scenarios.isEmpty { scenarios = Self.loadScenarios() }
        guard !scenarios.isEmpty else {
            Log.net.error("Mock mode: no fixtures found (set REIGNS_FIXTURES to shared/fixtures/scenarios)")
            return
        }
        generation += 1
        Log.net.info("Mock mode: replaying \(self.scenarios.count) fixture scenarios")
        play(index: 0, generation: generation)
    }

    func stop() {
        generation += 1  // cancels everything scheduled
        onScanning?(false)
    }

    // MARK: - Playback

    private func play(index: Int, generation: Int) {
        guard generation == self.generation else { return }
        let scenario = scenarios[index % scenarios.count]
        Log.net.info("Mock scenario: \(scenario.name, privacy: .public) — \(scenario.description, privacy: .public)")
        onNewChat?(scenario.name)

        var t: TimeInterval = 0.3
        for (stepIndex, step) in scenario.steps.enumerated() {
            if stepIndex > 0 {
                // A Claude reply was just "sent": think, then deliver its verdicts, heat and bubble.
                schedule(at: t, generation) { self.onScanning?(true) }
                t += Self.scanTime
                schedule(at: t, generation) { self.onScanning?(false) }
            }
            for message in step {
                schedule(at: t, generation) { self.engine.deliver(message) }
                t += Self.messageGap
            }
            t += stepIndex == 0 ? 1.5 : 4  // time to look at each reply's result
        }
        schedule(at: t + Self.holdAfterScenario, generation) {
            self.play(index: index + 1, generation: generation)
        }
    }

    private func schedule(at delay: TimeInterval, _ generation: Int, _ work: @escaping @MainActor () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, generation == self.generation else { return }
                work()
            }
        }
    }

    // MARK: - Fixtures

    private static func loadScenarios() -> [Scenario] {
        guard let directory = fixturesDirectory(),
              let files = try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: nil)
        else { return [] }

        let loaded: [(order: Int, scenario: Scenario)] = files
            .filter { $0.lastPathComponent.hasPrefix("scenario_") && $0.pathExtension == "json" }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url),
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let expected = json["expected"] as? [String: Any],
                      let replies = expected["engine_to_companion"] as? [[String: Any]]
                else { return nil }
                // Demo scenarios first, in demo-script order.
                let order = (json["demo_step"] as? Int) ?? 100
                let name = url.deletingPathExtension().lastPathComponent
                    .replacingOccurrences(of: "scenario_", with: "")
                return (order, Scenario(name: name, description: json["description"] as? String ?? "",
                                        steps: groupIntoSteps(replies)))
            }
        return loaded.sorted { ($0.order, $0.scenario.name) < ($1.order, $1.scenario.name) }.map(\.scenario)
    }

    /// Engine output is: heat.update (for session.start), then per reply verdicts.update → heat.update →
    /// bubble.content. Split it so each reply can be preceded by the thinking bubble.
    private static func groupIntoSteps(_ replies: [[String: Any]]) -> [[Data]] {
        var steps: [[Data]] = [[]]
        for reply in replies {
            if reply["type"] as? String == "verdicts.update" { steps.append([]) }
            if let data = try? JSONSerialization.data(withJSONObject: reply) {
                steps[steps.count - 1].append(data)
            }
        }
        return steps
    }

    /// REIGNS_FIXTURES, else look upward from the app for shared/fixtures/scenarios (the app is built
    /// inside the repo's companion/ folder).
    private static func fixturesDirectory() -> URL? {
        let fm = FileManager.default
        if let override = ProcessInfo.processInfo.environment["REIGNS_FIXTURES"] {
            return URL(fileURLWithPath: override)
        }
        var folder = Bundle.main.bundleURL
        for _ in 0..<8 {
            folder.deleteLastPathComponent()
            let candidate = folder.appendingPathComponent("shared/fixtures/scenarios")
            if fm.fileExists(atPath: candidate.path) { return candidate }
        }
        return nil
    }
}
