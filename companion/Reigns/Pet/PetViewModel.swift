import CoreGraphics
import Observation

/// Pet state (PRD §14 rule 9). FR-A6: level is driven only by heat.update.level once networking lands.
@MainActor
@Observable
final class PetViewModel {
    /// Charlie or Marley (menu › Character).
    var character = PetCharacter.saved
    /// True while switching characters (the old one has sunk out of view).
    var isCharacterHidden = false
    var level = 0
    var heat = 0
    /// Latest bubble.content from the engine; nil until the engine has said anything.
    var bubble: BubbleContent?
    /// heat.update.recovered: shown for 3 s after a verified fix (FR-A6 draws it).
    var isRecovered = false
    /// The engine is checking a reply Reigns just sent (shows the thinking bubble).
    var isScanning = false
    /// Screen point (Cocoa) between the eyes, so the pupils can follow the mouse. nil = look ahead.
    var eyeAnchor: CGPoint?
    /// The Dock pushed the pet up: show the whole horse instead of cutting it off at the Dock.
    var isOnDock = false
    /// heat.update.amber_count: claims the engine couldn't confirm (FR-A7 "?" badge).
    var unverifiedCount = 0
    /// Every checked claim in this conversation (merged from verdicts.update), for Details.
    var claims: [ClaimVerdict] = []
    /// Claims the user said were wrong ("I disagree"): struck through, never highlighted.
    var disagreedClaimIDs: Set<String> = []
    /// After an "I disagree", the engine rebuilds the fix without that claim; until the new bubble
    /// arrives, Fix it is held so it can't paste the old prompt.
    var isRebuildingFix = false
    /// FR-A9: Claude's message box already has text; waiting for Replace / Add to it.
    var pendingFix: Correction?
    /// Session Brief: the pet is offering to paste a context refresh (calm, non-alarming).
    var briefOffer: BriefOffer?
    /// The message box already has text; waiting for Replace / Add to it for the refresh.
    var pendingBrief: BriefOffer?
    /// FR-A9 fallback note shown in the bubble (e.g. "Press ⌘V in Claude").
    var fixNote: String?
    /// A new issue arrived that the user hasn't opened yet (shows the "Click me!" callout).
    var hasUnseenIssue = false
    /// The horse has spoken in this chat, so there's a line to replay.
    var hasVoiceLine = false
    var isBubbleOpen = false
    var isDetailsOpen = false
}
