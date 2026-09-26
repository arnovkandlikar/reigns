import Observation

/// Pet state (PRD §14 rule 9). FR-A6: level is driven only by heat.update.level once networking lands.
@MainActor
@Observable
final class PetViewModel {
    var level = 0
    var heat = 0
}
