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
                Text("Accessibility permission needed")
            }
            Text("Engine: \(state.engineStatus.rawValue)")
            Toggle("Mute Voice", isOn: $state.isVoiceMuted)
            #if DEBUG
            Menu("Preview Level") {
                ForEach(0..<5) { level in
                    Button("Level \(level)") { appDelegate.previewLevel(level) }
                }
                Button("Recovered") { appDelegate.previewRecovered() }
                Button("Scanning (thinking)") { appDelegate.previewScanning() }
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
    var isVoiceMuted = UserDefaults.standard.bool(forKey: "voiceMuted") {
        didSet { UserDefaults.standard.set(isVoiceMuted, forKey: "voiceMuted") }
    }
}
