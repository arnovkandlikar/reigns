import SwiftUI

/// What the bubble's buttons do; wired up by PetPanelController.
struct BubbleActions {
    var fixIt: (Correction) -> Void
    var disagree: (BubbleProblem) -> Void
    var dismiss: () -> Void
}

/// FR-A8: speech bubble above the pet. Shows bubble.content with Fix it (only when a correction is
/// offered), Details, I disagree and Dismiss. Max width 320 px.
struct BubbleView: View {
    let model: PetViewModel
    let actions: BubbleActions
    /// Which side the pet is on; the tail points down at it from that side.
    let tailEdge: HorizontalEdge

    static let width: CGFloat = 320
    /// Distance from the bubble's edge to the tail's centre (the pet's centre).
    static let tailInset: CGFloat = PetView.panelSize.width / 2

    private var content: BubbleContent { model.bubble ?? .allClear }

    var body: some View {
        VStack(alignment: tailEdge == .trailing ? .trailing : .leading, spacing: 0) {
            card
            BubbleTail()
                .fill(.regularMaterial)
                .frame(width: 20, height: 10)
                .padding(tailEdge == .trailing ? .trailing : .leading, Self.tailInset - 10)
        }
        .frame(width: Self.width)
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Text(content.headline)
                .font(.system(size: 14, weight: .semibold))
                .fixedSize(horizontal: false, vertical: true)

            if !content.problems.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(content.problems.prefix(3)) { problem in
                        Text("• \(problem.text)")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .font(.system(size: 12))
            }

            if !content.patternText.isEmpty {
                (Text("How it's going off course: ").bold() + Text(content.patternText))
                    .font(.system(size: 12))
                    .fixedSize(horizontal: false, vertical: true)
            }

            if model.isDetailsOpen {
                BubbleDetails(claims: model.claims, problems: content.problems)
            }

            if !content.actionText.isEmpty {
                Text(content.actionText)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            buttons
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(PetLevel.color(content.level).opacity(0.6), lineWidth: 1.5))
    }

    private var header: some View {
        HStack(spacing: 6) {
            Circle().fill(PetLevel.color(content.level)).frame(width: 8, height: 8)
            Text(PetLevel.name(content.level))
                .font(.system(size: 11, weight: .semibold))
            Spacer()
            if !content.confidenceLabel.isEmpty {
                Text(content.confidenceLabel)
                    .font(.system(size: 11, weight: .medium))
                    .help(content.confidenceReason)
            }
        }
        .foregroundStyle(.secondary)
    }

    private var buttons: some View {
        HStack(spacing: 6) {
            if let correction = content.correction {
                Button("Fix it") { actions.fixIt(correction) }
                    .buttonStyle(.borderedProminent)
                    .tint(PetLevel.color(content.level))
            }
            Button(model.isDetailsOpen ? "Hide details" : "Details") {
                model.isDetailsOpen.toggle()
            }
            Button("I disagree") {
                if let top = content.problems.first { actions.disagree(top) }
            }
            .disabled(content.problems.isEmpty)
            Spacer(minLength: 0)
            Button("Dismiss") { actions.dismiss() }
        }
        .controlSize(.small)
    }
}

/// FR-A8 Details: every checked claim in the conversation with its verdict, what the detectors
/// found, and evidence links. Problems first (red, then amber), then claims that checked out.
private struct BubbleDetails: View {
    let claims: [ClaimVerdict]
    /// Fallback before any verdicts.update has arrived (e.g. debug previews).
    let problems: [BubbleProblem]

    private static let order = ["red": 0, "amber": 1, "green": 2]

    private var shown: [ClaimVerdict] {
        claims
            .filter { Self.order[$0.final] != nil }  // "skipped" claims weren't checked
            .sorted { Self.order[$0.final]! < Self.order[$1.final]! }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            if shown.isEmpty {
                if problems.isEmpty {
                    Text("No checked claims in this conversation yet.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
                ForEach(problems) { problem in
                    ClaimRow(verdict: nil, quote: problem.text, explanation: nil,
                             links: problem.evidenceURL.map { [("Evidence", $0)] } ?? [])
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(shown) { claim in
                            ClaimRow(verdict: claim.final, quote: claim.quote,
                                     explanation: Self.explanation(for: claim), links: Self.links(for: claim))
                        }
                    }
                }
                .frame(maxHeight: 260)
            }
            Divider()
        }
    }

    /// The detector note that decided the verdict: prefer a non-error result that isn't just
    /// "supported"/"consistent" when the claim was flagged.
    private static func explanation(for claim: ClaimVerdict) -> String? {
        let useful = claim.detectorResults.filter { $0.status != "error" && !$0.explanation.isEmpty }
        let flagged = useful.first { !["supported", "consistent"].contains($0.status) }
        return (claim.final == "green" ? useful.first : flagged ?? useful.first)?.explanation
    }

    private static func links(for claim: ClaimVerdict) -> [(String, String)] {
        var seen = Set<String>()
        return claim.detectorResults.flatMap(\.evidence).compactMap { evidence in
            guard let url = evidence.url, seen.insert(url).inserted else { return nil }
            return (evidence.source, url)
        }
        .prefix(3).map { $0 }
    }
}

private struct ClaimRow: View {
    /// "red" | "amber" | "green", or nil for a plain problem line.
    let verdict: String?
    let quote: String
    let explanation: String?
    let links: [(String, String)]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let verdict {
                HStack(spacing: 5) {
                    Circle().fill(Self.color(verdict)).frame(width: 7, height: 7)
                    Text(Self.label(verdict)).font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                Text("\u{201C}\(quote)\u{201D}")
                    .font(.system(size: 12))
                    .italic()
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(quote).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            }
            if let explanation {
                Text(explanation)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                ForEach(links, id: \.1) { source, url in
                    if let link = URL(string: url) {
                        Link("\(source) ↗", destination: link).font(.system(size: 11))
                    }
                }
            }
        }
    }

    // Text label alongside the colour so colour is never the only signal (§18).
    private static func label(_ verdict: String) -> String {
        switch verdict {
        case "red": return "LIKELY WRONG"
        case "amber": return "COULDN'T CONFIRM"
        default: return "CHECKED OUT"
        }
    }

    private static func color(_ verdict: String) -> Color {
        switch verdict {
        case "red": return .red
        case "amber": return .orange
        default: return .green
        }
    }
}

private struct BubbleTail: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.minX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.midX, y: rect.maxY))
            p.closeSubpath()
        }
    }
}

/// Level names and colours (§9 / §10). Colour is always paired with the name and heat number.
enum PetLevel {
    static func name(_ level: Int) -> String {
        ["Calm", "Curious", "Concerned", "Alarmed", "Meltdown"][min(max(level, 0), 4)]
    }

    static func color(_ level: Int) -> Color {
        switch level {
        case ..<1: return .green
        case 1: return .yellow
        case 2: return .orange
        case 3: return .red
        default: return .purple
        }
    }
}
