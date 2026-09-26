import SwiftUI

/// FR-A10: explains why Reigns needs Accessibility permission and gets the user there.
struct OnboardingView: View {
    let isTrusted: Bool
    let openSettings: () -> Void
    let later: () -> Void

    @State private var calmHorse = PetViewModel()

    var body: some View {
        VStack(spacing: 0) {
            // The horse peeking over the top edge of the card, like it does on Claude's window.
            PetView(model: calmHorse, showsHeatBadge: false)
                .scaleEffect(0.55, anchor: .bottom)
                .frame(height: 70, alignment: .bottom)
                .clipped()
                .allowsHitTesting(false)

            VStack(alignment: .leading, spacing: 14) {
                Text("Reigns keeps an eye on Claude")
                    .font(.system(size: 20, weight: .bold))
                Text("To read Claude's replies and catch made-up facts, Reigns needs macOS **Accessibility** permission.")
                    .fixedSize(horizontal: false, vertical: true)

                VStack(alignment: .leading, spacing: 8) {
                    Promise(icon: "eye", text: "Only reads the Claude app, nothing else on your Mac.")
                    Promise(icon: "cursorarrow.click", text: "Only types when you click **Fix it**, and never presses Enter.")
                    Promise(icon: "paperplane", text: "Never sends a message for you. You stay in control.")
                    Promise(icon: "lock", text: "Checks run on your Mac; web searches only see the claim being checked.")
                }

                Divider()

                VStack(alignment: .leading, spacing: 4) {
                    Text("1. Click **Open Accessibility Settings**.")
                    Text("2. Turn on **Reigns** in the list.")
                    Text("3. Come back. Reigns starts on its own.")
                }
                .font(.system(size: 12))
                .foregroundStyle(.secondary)

                status

                HStack {
                    Button("Later", action: later)
                        .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button("Open Accessibility Settings", action: openSettings)
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                        .disabled(isTrusted)
                }
            }
            .padding(24)
            .background(.background, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .padding(.horizontal, 20)
        .padding(.bottom, 20)
        .frame(width: 440)
    }

    @ViewBuilder private var status: some View {
        if isTrusted {
            Label("All set! Reigns is watching Claude.", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .font(.system(size: 13, weight: .semibold))
        } else {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Waiting for permission…").foregroundStyle(.secondary)
            }
            .font(.system(size: 12))
        }
    }
}

private struct Promise: View {
    let icon: String
    let text: LocalizedStringKey

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: icon)
                .frame(width: 18)
                .foregroundStyle(.tint)
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: 13))
    }
}
