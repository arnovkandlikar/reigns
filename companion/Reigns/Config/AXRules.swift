import Foundation

/// Element-matching rules and tunables loaded from AXRules.json (PRD R8: Claude app updates
/// should only need a config change, not a code change).
struct AXRules: Decodable {
    var claudeBundleID: String
    var petInsetPx: Double
    var windowPollMs: Int

    private enum CodingKeys: String, CodingKey {
        case claudeBundleID = "claude_bundle_id"
        case petInsetPx = "pet_inset_px"
        case windowPollMs = "window_poll_ms"
    }

    static let fallback = AXRules(
        claudeBundleID: "com.anthropic.claudefordesktop", petInsetPx: 24, windowPollMs: 250)

    static func load() -> AXRules {
        guard let url = Bundle.main.url(forResource: "AXRules", withExtension: "json"),
              let data = try? Data(contentsOf: url)
        else {
            Log.app.error("AXRules.json missing from bundle; using fallback rules")
            return .fallback
        }
        do {
            return try JSONDecoder().decode(AXRules.self, from: data)
        } catch {
            Log.app.error("AXRules.json invalid (\(String(describing: error), privacy: .public)); using fallback rules")
            return .fallback
        }
    }
}
