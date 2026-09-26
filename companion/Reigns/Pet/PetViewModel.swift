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
    /// Every checked claim in this conversation (merged from verdicts.update), for Details.
    var claims: [ClaimVerdict] = []
    var isBubbleOpen = false
    var isDetailsOpen = false
}
