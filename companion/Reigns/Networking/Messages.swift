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

/// heat.update payload (shared/schemas/heat_update.json).
struct HeatUpdate: Decodable, Equatable {
    var heat: Int
    var level: Int
    var recovered: Bool = false
    var redCount: Int = 0
    var amberCount: Int = 0

    private enum CodingKeys: String, CodingKey {
        case heat, level, recovered
        case redCount = "red_count"
        case amberCount = "amber_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        heat = try c.decode(Int.self, forKey: .heat)
        level = try c.decode(Int.self, forKey: .level)
        recovered = try c.decodeIfPresent(Bool.self, forKey: .recovered) ?? false
        redCount = try c.decodeIfPresent(Int.self, forKey: .redCount) ?? 0
        amberCount = try c.decodeIfPresent(Int.self, forKey: .amberCount) ?? 0
    }
}

/// verdicts.update payload (shared/schemas/verdicts_update.json).
struct VerdictsUpdate: Decodable, Equatable {
    var messageID: String
    var claims: [ClaimVerdict]

    private enum CodingKeys: String, CodingKey {
        case claims
        case messageID = "message_id"
    }
}

struct ClaimVerdict: Decodable, Equatable, Identifiable {
    var claimID: String
    var quote: String
    var type: String
    var risk: String
    /// "red" | "amber" | "green" | "skipped"
    var final: String
    var detectorResults: [DetectorResult]

    var id: String { claimID }

    private enum CodingKeys: String, CodingKey {
        case quote, type, risk, final
        case claimID = "claim_id"
        case detectorResults = "detector_results"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        claimID = try c.decode(String.self, forKey: .claimID)
        quote = try c.decode(String.self, forKey: .quote)
        type = try c.decode(String.self, forKey: .type)
        risk = try c.decode(String.self, forKey: .risk)
        final = try c.decode(String.self, forKey: .final)
        detectorResults = try c.decodeIfPresent([DetectorResult].self, forKey: .detectorResults) ?? []
    }
}

struct DetectorResult: Decodable, Equatable {
    var detector: String
    var status: String
    var confidence: Double
    var evidence: [Evidence]
    var explanation: String

    private enum CodingKeys: String, CodingKey {
        case detector, status, confidence, evidence, explanation
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        detector = try c.decode(String.self, forKey: .detector)
        status = try c.decode(String.self, forKey: .status)
        confidence = try c.decode(Double.self, forKey: .confidence)
        evidence = try c.decodeIfPresent([Evidence].self, forKey: .evidence) ?? []
        explanation = try c.decode(String.self, forKey: .explanation)
    }
}

struct Evidence: Decodable, Equatable {
    var source: String
    var url: String?
    var snippet: String
}

/// error payload (shared/schemas/error.json).
struct EngineError: Decodable {
    var code: String
    var message: String
}

// §12.1 Companion → Engine payloads.

struct SessionStartPayload: Encodable {
    var app = "claude"
    var appVersion: String
    var companionVersion: String
    /// message_id of the chat's first message (position 0), so the engine can restore that chat's
    /// heat when the user flips back to it. Left out of the JSON when unknown.
    var chatKey: String?
    /// Language the pet speaks: "en" or "es".
    var language: String?
    /// Which pet is on screen, for its voice and personality: "charlie" or "marley".
    var character: String?

    private enum CodingKeys: String, CodingKey {
        case app
        case appVersion = "app_version"
        case companionVersion = "companion_version"
        case chatKey = "chat_key"
        case language
        case character
    }
}

/// session.update payload (shared/schemas/session_update.json): switch language / pet mid-chat.
/// Same session, so the score, history and bubble carry over. Missing fields are left unchanged.
struct SessionUpdatePayload: Encodable {
    var language: String?
    var character: String?
}

struct MessageNewPayload: Encodable {
    var messageID: String
    var role: String
    var text: String
    var position: Int

    private enum CodingKeys: String, CodingKey {
        case role, text, position
        case messageID = "message_id"
    }
}

struct FeedbackDisagreePayload: Encodable {
    var claimID: String
    var note: String?

    private enum CodingKeys: String, CodingKey {
        case note
        case claimID = "claim_id"
    }
}

struct CorrectionInsertedPayload: Encodable {
    var correctionID: String

    private enum CodingKeys: String, CodingKey {
        case correctionID = "correction_id"
    }
}

/// voice.play payload (shared/schemas/voice_play.json).
struct VoicePlay: Decodable {
    var text: String
    var audioB64: String
    var level: Int

    private enum CodingKeys: String, CodingKey {
        case text, level
        case audioB64 = "audio_b64"
    }
}

/// brief.offer payload (Session Brief, Role C/B): the pet offers to paste a context refresh into
/// Claude's message box. Only sent while the pet is calm (level 0–1).
struct BriefOffer: Decodable, Equatable {
    /// "long_chat" | "forgot"
    var reason: String
    var headline: String
    /// Button label, e.g. "Paste a context refresh".
    var action: String
    /// The refresh text to paste.
    var text: String
    var turns: Int

    private enum CodingKeys: String, CodingKey {
        case reason, headline, action, text, turns
    }

    init(reason: String, headline: String, action: String, text: String, turns: Int) {
        self.reason = reason
        self.headline = headline
        self.action = action
        self.text = text
        self.turns = turns
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? "long_chat"
        headline = try c.decode(String.self, forKey: .headline)
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? "Paste a context refresh"
        text = try c.decode(String.self, forKey: .text)
        turns = try c.decodeIfPresent(Int.self, forKey: .turns) ?? 0
    }

    #if DEBUG
    /// For the debug menu / demo (no engine needed).
    static let sample = BriefOffer(
        reason: "long_chat",
        headline: "This chat is getting long. Want me to remind Claude what matters so far?",
        action: "Paste a context refresh",
        text: "Quick recap before we continue: we're building a Salesforce HR MVP (job openings, candidate pipeline, interviews, onboarding tasks). Decisions so far: custom objects Job_Opening__c, Candidate__c and Interview__c; a Flow creates onboarding tasks on Hired; the React app talks to Salesforce through the MCP server. Please keep these in mind for the next steps.",
        turns: 24)
    #endif
}
