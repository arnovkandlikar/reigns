import SwiftUI

@main
struct ReignsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var state = AppState.shared

    var body: some Scene {
        // PRD §18 privacy: pause switch in the menu bar.
        MenuBarExtra("Reigns", systemImage: state.isPaused ? "eye.slash" : "eye") {
            Button(state.isPaused ? "Resume Reigns" : "Pause Reigns") {
                appDelegate.togglePause()
            }
            if !state.axTrusted {
                Button("Set Up Accessibility…") { appDelegate.showOnboarding() }  // FR-A10
            }
            Text("Engine: \(state.engineStatus.rawValue)")
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
                Toggle("Mock Engine (fixtures)", isOn: Binding(
                    get: { state.isMockEngine },
                    set: { appDelegate.setMockEngine($0) }))
            }
            Button("Log Conversation") { appDelegate.logConversation() }
            #endif
            Divider()
            Button("Quit Reigns") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
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
    var isVoiceMuted = UserDefaults.standard.bool(forKey: "voiceMuted") {
        didSet { UserDefaults.standard.set(isVoiceMuted, forKey: "voiceMuted") }
    }
}
