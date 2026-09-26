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
                BubbleDetails(problems: content.problems)
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

/// Expanded per-claim list with evidence links. (Once verdicts.update is wired up this can show every
/// checked claim, not just the bubble's problems.)
private struct BubbleDetails: View {
    let problems: [BubbleProblem]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            if problems.isEmpty {
                Text("No flagged claims in this conversation yet.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            ForEach(problems) { problem in
                VStack(alignment: .leading, spacing: 2) {
                    Text(problem.text)
                        .font(.system(size: 12))
                        .fixedSize(horizontal: false, vertical: true)
                    if let link = problem.evidenceURL.flatMap(URL.init(string:)) {
                        Link("Evidence ↗", destination: link)
                            .font(.system(size: 11))
                    } else {
                        Text("No source link")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            Divider()
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
