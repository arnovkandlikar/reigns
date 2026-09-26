// Dev harness for FR-A3: runs the app's ConversationReader against the live Claude window.
// Usage: sh Tools/read_conversation.sh [--full]
import AppKit

let full = CommandLine.arguments.contains("--full")
let rulesURL = URL(fileURLWithPath: CommandLine.arguments[1])
let rules = try JSONDecoder().decode(AXRules.self, from: Data(contentsOf: rulesURL))
guard let claude = NSRunningApplication.runningApplications(withBundleIdentifier: rules.claudeBundleID).first
else { fatalError("Claude not running") }

ConversationReader.enableAccessibility(pid: claude.processIdentifier)
let start = Date()
let messages = ConversationReader(rules: rules.conversation).read(pid: claude.processIdentifier)
let ms = Int(Date().timeIntervalSince(start) * 1000)

for m in messages {
    let text = full ? m.text : String(m.text.replacingOccurrences(of: "\n", with: " ⏎ ").prefix(110))
    print("[\(m.position)] \(m.role.rawValue.padding(toLength: 9, withPad: " ", startingAt: 0)) \(text)")
}
print("— \(messages.count) messages in \(ms) ms")
