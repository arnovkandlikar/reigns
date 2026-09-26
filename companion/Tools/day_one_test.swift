// Witness Day-One Test (PRD §6 Role A, §16 h0–2, §19 Q5)
// Answers: can we READ Claude's chat via AX? can we PASTE into its message box? what's the bundle ID?
// Usage: swift day_one_test.swift            (read-only)
//        swift day_one_test.swift --paste    (also pastes a test string; never presses Enter)
import AppKit
import ApplicationServices

let bundleID = "com.anthropic.claudefordesktop"
let doPaste = CommandLine.arguments.contains("--paste")
let dumpPath = "day_one_dump.txt"

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}
func str(_ el: AXUIElement, _ name: String) -> String? {
    if let s = attr(el, name) as? String, !s.isEmpty { return s }
    return nil
}
func frame(_ el: AXUIElement) -> CGRect? {
    guard let p = attr(el, kAXPositionAttribute), let s = attr(el, kAXSizeAttribute) else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    AXValueGetValue(p as! AXValue, .cgPoint, &pt)
    AXValueGetValue(s as! AXValue, .cgSize, &sz)
    return CGRect(origin: pt, size: sz)
}
func oneLine(_ s: String, _ n: Int = 120) -> String {
    let t = s.replacingOccurrences(of: "\n", with: "⏎ ")
    return t.count > n ? String(t.prefix(n)) + "…" : t
}

// 1. Bundle ID / running app
print("== 1. App ==")
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
    print("FAIL: Claude app (\(bundleID)) not running"); exit(1)
}
print("PASS bundle ID: \(bundleID)  pid: \(app.processIdentifier)  version: \(app.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary?["CFBundleShortVersionString"] as? String } ?? "?")")

// 2. Accessibility permission
print("\n== 2. Accessibility permission ==")
let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
print(trusted ? "PASS process is AX-trusted" : "FAIL not AX-trusted — grant in System Settings › Privacy & Security › Accessibility, then rerun")
if !trusted { exit(2) }

// 3. Enable Chromium/Electron accessibility tree (FR-A3)
print("\n== 3. AXManualAccessibility ==")
let appEl = AXUIElementCreateApplication(app.processIdentifier)
let r = AXUIElementSetAttributeValue(appEl, "AXManualAccessibility" as CFString, kCFBooleanTrue)
print("set AXManualAccessibility → \(r == .success ? "PASS" : "result \(r.rawValue)")")
Thread.sleep(forTimeInterval: 1.5) // give Chromium time to build the tree

// 4. Walk tree
print("\n== 4. AX tree walk ==")
var dump: [String] = []
var texts: [(depth: Int, role: String, classes: String, text: String)] = []
var inputs: [(el: AXUIElement, desc: String)] = []
var nodes = 0
func walk(_ el: AXUIElement, _ depth: Int) {
    guard nodes < 30000, depth < 80 else { return }
    nodes += 1
    let role = str(el, kAXRoleAttribute) ?? "?"
    let sub = str(el, kAXSubroleAttribute) ?? ""
    let classes = (attr(el, "AXDOMClassList") as? [String])?.joined(separator: ".") ?? ""
    let domID = str(el, "AXDOMIdentifier") ?? ""
    let value = str(el, kAXValueAttribute) ?? ""
    let title = str(el, kAXTitleAttribute) ?? str(el, kAXDescriptionAttribute) ?? ""
    var line = String(repeating: "  ", count: depth) + role
    if !sub.isEmpty { line += "[\(sub)]" }
    if !domID.isEmpty { line += " #\(domID)" }
    if !classes.isEmpty { line += " .\(oneLine(classes, 80))" }
    if !title.isEmpty { line += " title=\"\(oneLine(title, 60))\"" }
    if !value.isEmpty { line += " value=\"\(oneLine(value))\"" }
    dump.append(line)
    if role == "AXStaticText", !value.isEmpty { texts.append((depth, role, classes, value)) }
    var editable: AnyObject?
    let isEditable = AXUIElementCopyAttributeValue(el, "AXEditable" as CFString, &editable) == .success && (editable as? Bool) == true
    if role == "AXTextArea" || (role == "AXTextField") || (isEditable && role != "AXStaticText" && role != "AXGroup") {
        inputs.append((el, "\(role) \(classes.isEmpty ? "" : ".\(oneLine(classes, 60)) ")frame=\(frame(el).map { "\($0)" } ?? "?") value=\"\(oneLine(value, 60))\""))
    }
    if let kids = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for k in kids { walk(k, depth + 1) }
    }
}
walk(appEl, 0)
try? dump.joined(separator: "\n").write(toFile: dumpPath, atomically: true, encoding: .utf8)
print("nodes visited: \(nodes)   static text nodes: \(texts.count)   full dump → \(dumpPath)")
print(texts.count > 5 ? "PASS can read chat text" : "FAIL little/no text visible (R1 fallback: OCR / Chrome extension)")
print("\nLast 15 text nodes (depth · classes · text):")
for t in texts.suffix(15) { print("  d\(t.depth) · \(oneLine(t.classes, 50)) · \(oneLine(t.text, 90))") }

print("\n== 5. Input candidates (message box) ==")
for (i, inp) in inputs.enumerated() { print("  [\(i)] \(inp.desc)") }
if inputs.isEmpty { print("FAIL no editable element found") }

// 6. Paste test (FR-A9) — opt-in; never presses Enter
print("\n== 6. Paste test ==")
guard doPaste else { print("skipped (rerun with --paste)"); exit(0) }
guard let target = inputs.last?.el else { print("FAIL no input to paste into"); exit(3) }
let testText = "[Witness day-one paste test — safe to delete]"
let pb = NSPasteboard.general
let saved = pb.string(forType: .string)
app.activate()
Thread.sleep(forTimeInterval: 0.4)
let focusRes = AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
print("AX focus → \(focusRes == .success ? "ok" : "result \(focusRes.rawValue)")")
pb.clearContents(); pb.setString(testText, forType: .string)
let src = CGEventSource(stateID: .combinedSessionState)
let vDown = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: true)!  // 'v'
let vUp = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: false)!
vDown.flags = .maskCommand; vUp.flags = .maskCommand
vDown.post(tap: .cghidEventTap); vUp.post(tap: .cghidEventTap)
Thread.sleep(forTimeInterval: 0.3)
pb.clearContents(); if let saved { pb.setString(saved, forType: .string) }
Thread.sleep(forTimeInterval: 0.5)
let after = str(target, kAXValueAttribute) ?? ""
print(after.contains("Witness day-one") ? "PASS paste landed in message box" : "UNCONFIRMED paste — check Claude's box visually (AX value: \"\(oneLine(after, 80))\")")
print("clipboard restored: \(pb.string(forType: .string) == saved ? "yes" : "NO")")
