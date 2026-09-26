#if DEBUG
/// Sample bubble.content per level for previewing the bubble before the engine is connected
/// (level 3 mirrors shared/fixtures/messages/bubble_content.json).
extension BubbleContent {
    static func preview(level: Int) -> BubbleContent {
        switch level {
        case ..<1:
            return .allClear
        case 1:
            return BubbleContent(
                level: 1, headline: "Hmm, I couldn't confirm two claims.",
                problems: [
                    BubbleProblem(claimID: "p1", text: "The 2019 survey figure of 62% didn't turn up anywhere.", evidenceURL: nil),
                    BubbleProblem(claimID: "p2", text: "I couldn't find the quote attributed to the WHO report.", evidenceURL: "https://www.who.int"),
                ],
                confidenceLabel: "Not sure", confidenceReason: "Search found nothing either way.",
                actionText: "Want me to ask Claude to double-check?",
                correction: Correction(
                    correctionID: "c1", promptType: .verifyNudge,
                    text: "Quick check: can you confirm the 62% figure and the WHO quote? If you're not sure, say so."))
        case 2:
            return BubbleContent(
                level: 2, headline: "Claude may be guessing on specifics.",
                problems: [
                    BubbleProblem(claimID: "p3", text: "It said the API limit is 500 requests/min; the docs say 100.", evidenceURL: "https://docs.example.com/limits"),
                    BubbleProblem(claimID: "p4", text: "Its answers about the release year kept changing.", evidenceURL: nil),
                ],
                patternText: "It's filling gaps with confident numbers.",
                confidenceLabel: "Fairly sure", confidenceReason: "Checked the official docs.",
                actionText: "I wrote a short correction.",
                correction: Correction(
                    correctionID: "c2", promptType: .targetedCorrection,
                    text: "You said the API limit is 500 requests/min; the official docs say 100. Please re-check and redo only the steps that depend on it."))
        case 3:
            return BubbleContent(
                level: 3, headline: "Heads up: Claude is making up sources.",
                problems: [
                    BubbleProblem(claimID: "p5", text: "The paper \"Lee & Park, 2022\" doesn't exist.",
                                  evidenceURL: "https://api.crossref.org/works?query.bibliographic=Lee+Park+2022"),
                    BubbleProblem(claimID: "p6", text: "\"HiveFormer\" (Moreau et al., 2021) doesn't exist.", evidenceURL: nil),
                    BubbleProblem(claimID: "p7", text: "\"Okafor et al., 2023\" doesn't exist.", evidenceURL: nil),
                ],
                patternText: "It's filling gaps with confident guesses instead of saying it doesn't know.",
                confidenceLabel: "Very sure", confidenceReason: "Checked Crossref and Semantic Scholar.",
                actionText: "I wrote a prompt to fix this.",
                correction: Correction(
                    correctionID: "c3", promptType: .diagnosticReset,
                    text: "I want to pause and fix a few problems before we continue."))
        default:
            return BubbleContent(
                level: 4, headline: "This conversation has gone off the rails. Start fresh?",
                problems: [
                    BubbleProblem(claimID: "p8", text: "Four cited papers don't exist.", evidenceURL: nil),
                    BubbleProblem(claimID: "p9", text: "Steps 3–7 build on a wrong rate limit.", evidenceURL: nil),
                    BubbleProblem(claimID: "p10", text: "It reversed a correct answer when pushed back on.", evidenceURL: nil),
                ],
                patternText: "Errors are stacking on each other; patching won't fix it.",
                confidenceLabel: "Very sure", confidenceReason: "Several independent checks agree.",
                actionText: "I wrote a hand-off to start a new chat cleanly.",
                correction: Correction(
                    correctionID: "c4", promptType: .freshStart,
                    text: "Summary for a fresh chat: …"))
        }
    }
}
#endif
