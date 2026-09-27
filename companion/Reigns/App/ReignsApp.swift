import SwiftUI

@main
struct ReignsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var state = AppState.shared

    var body: some Scene {
        // PRD §18 privacy: pause switch in the menu bar.
        MenuBarExtra {
            Button(state.isPaused ? "Resume Reigns" : "Pause Reigns") {
                appDelegate.togglePause()
            }
            if !state.axTrusted {
                Button("Set Up Accessibility…") { appDelegate.showOnboarding() }  // FR-A10
            }
            Picker("Character", selection: Binding(
                get: { state.character },
                set: { appDelegate.setCharacter($0) })) {
                ForEach(PetCharacter.allCases) { character in
                    Text(character.displayName).tag(character)
                }
            }
            Picker("Language", selection: Binding(
                get: { state.language },
                set: { appDelegate.setLanguage($0) })) {
                ForEach(PetLanguage.allCases) { language in
                    Text(language.displayName).tag(language)
                }
            }
            Text("Engine: \(state.engineStatus.rawValue)")
            Toggle("Highlight Problems", isOn: $state.isHighlighting)
            Toggle("Mute Voice", isOn: $state.isVoiceMuted)
                .onChange(of: state.isVoiceMuted) { _, muted in
                    if muted { appDelegate.stopVoice() }
                }
            #if DEBUG
            Menu("Preview Level") {
                ForEach(0..<5) { level in
                    Button("Level \(level)") { appDelegate.previewLevel(level) }
                }
                Button("Recovered") { appDelegate.previewRecovered() }
                Button("Scanning (thinking)") { appDelegate.previewScanning() }
                Button("Onboarding Window") { appDelegate.showOnboarding() }
                Button("Context Refresh Offer") { appDelegate.previewBriefOffer() }
                Toggle("Mock Engine (fixtures)", isOn: Binding(
                    get: { state.isMockEngine },
                    set: { appDelegate.setMockEngine($0) }))
            }
            Button("Log Conversation") { appDelegate.logConversation() }
            #endif
            Divider()
            Button("Quit Reigns") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
        } label: {
            // The reins icon (template: macOS colours it for light/dark menu bars). Faded when paused.
            Image(nsImage: MenuBarIcon.image(paused: state.isPaused))
        }
    }
}

/// App-wide UI state shared by the menu bar and the delegate.
@MainActor
@Observable
final class AppState {
    static let shared = AppState()
    var isPaused = false
    var axTrusted = false
    var engineStatus = EngineClient.Status.offline
    /// FR-A11: replaying fixtures instead of the engine.
    var isMockEngine = false
    var character = PetCharacter.saved
    /// Mark hallucinated text on Claude's window.
    var isHighlighting = UserDefaults.standard.object(forKey: "highlightProblems") as? Bool ?? true {
        didSet { UserDefaults.standard.set(isHighlighting, forKey: "highlightProblems") }
    }
    var language = PetLanguage.saved
    var isVoiceMuted = UserDefaults.standard.bool(forKey: "voiceMuted") {
        didSet { UserDefaults.standard.set(isVoiceMuted, forKey: "voiceMuted") }
    }
}
