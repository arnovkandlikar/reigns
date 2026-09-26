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
            #if DEBUG
            Menu("Preview Level") {
                ForEach(0..<5) { level in
                    Button("Level \(level)") { appDelegate.previewLevel(level) }
                }
            }
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
}
