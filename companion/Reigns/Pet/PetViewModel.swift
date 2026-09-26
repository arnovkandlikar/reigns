import Observation

/// Pet state (PRD §14 rule 9). FR-A6: level is driven only by heat.update.level once networking lands.
@MainActor
@Observable
final class PetViewModel {
    var level = 0
    var heat = 0
    /// Latest bubble.content from the engine; nil until the engine has said anything.
    var bubble: BubbleContent?
    /// heat.update.recovered: shown for 3 s after a verified fix (FR-A6 draws it).
    var isRecovered = false
    /// The engine is checking a reply Reigns just sent (shows the thinking bubble).
    var isScanning = false
    /// heat.update.amber_count: claims the engine couldn't confirm (FR-A7 "?" badge).
    var unverifiedCount = 0
    /// Every checked claim in this conversation (merged from verdicts.update), for Details.
    var claims: [ClaimVerdict] = []
    /// FR-A9: Claude's message box already has text; waiting for Replace / Add to it.
    var pendingFix: Correction?
    /// FR-A9 fallback note shown in the bubble (e.g. "Press ⌘V in Claude").
    var fixNote: String?
    var isBubbleOpen = false
    var isDetailsOpen = false
}
