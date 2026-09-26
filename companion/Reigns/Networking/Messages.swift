import Foundation

// §12.2 Engine → Companion payloads. Field names mirror shared/schemas/*.json exactly; change only
// through the §15.3 contract process (Role B updates the schemas first).

/// bubble.content payload (shared/schemas/bubble_content.json).
struct BubbleContent: Decodable, Equatable {
    var level: Int
    var headline: String
    var problems: [BubbleProblem] = []
    var patternText: String = ""
    var confidenceLabel: String = ""
    var confidenceReason: String = ""
    var actionText: String = ""
    /// nil at level 0.
    var correction: Correction?

    private enum CodingKeys: String, CodingKey {
        case level, headline, problems, correction
        case patternText = "pattern_text"
        case confidenceLabel = "confidence_label"
        case confidenceReason = "confidence_reason"
        case actionText = "action_text"
    }

    init(level: Int, headline: String, problems: [BubbleProblem] = [], patternText: String = "",
         confidenceLabel: String = "", confidenceReason: String = "", actionText: String = "",
         correction: Correction? = nil) {
        self.level = level
        self.headline = headline
        self.problems = problems
        self.patternText = patternText
        self.confidenceLabel = confidenceLabel
        self.confidenceReason = confidenceReason
        self.actionText = actionText
        self.correction = correction
    }

    // The schema gives most fields defaults, so decode them leniently.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        level = try c.decode(Int.self, forKey: .level)
        headline = try c.decode(String.self, forKey: .headline)
        problems = try c.decodeIfPresent([BubbleProblem].self, forKey: .problems) ?? []
        patternText = try c.decodeIfPresent(String.self, forKey: .patternText) ?? ""
        confidenceLabel = try c.decodeIfPresent(String.self, forKey: .confidenceLabel) ?? ""
        confidenceReason = try c.decodeIfPresent(String.self, forKey: .confidenceReason) ?? ""
        actionText = try c.decodeIfPresent(String.self, forKey: .actionText) ?? ""
        correction = try c.decodeIfPresent(Correction.self, forKey: .correction)
    }

    /// Shown before the engine has said anything about this conversation.
    static let allClear = BubbleContent(
        level: 0, headline: "All clear so far.",
        actionText: "Nothing to fix right now. I'll speak up if something looks off.")
}

struct BubbleProblem: Decodable, Equatable, Identifiable {
    var claimID: String
    var text: String
    var evidenceURL: String?

    var id: String { claimID }

    private enum CodingKeys: String, CodingKey {
        case text
        case claimID = "claim_id"
        case evidenceURL = "evidence_url"
    }
}

struct Correction: Decodable, Equatable {
    enum PromptType: String, Decodable {
        case verifyNudge = "verify_nudge"
        case targetedCorrection = "targeted_correction"
        case diagnosticReset = "diagnostic_reset"
        case freshStart = "fresh_start"
    }

    var correctionID: String
    var promptType: PromptType
    var text: String

    private enum CodingKeys: String, CodingKey {
        case text
        case correctionID = "correction_id"
        case promptType = "prompt_type"
    }
}
