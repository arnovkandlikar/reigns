import AppKit

/// The menu bar icon: the Reigns waves logo (Assets › MenuBarIcon, a template image), sized for
/// the menu bar and faded while Reigns is paused.
enum MenuBarIcon {
    private static let height: CGFloat = 18

    static func image(paused: Bool) -> NSImage {
        guard let source = NSImage(named: "MenuBarIcon") else {
            // Fallback so the menu is never unreachable.
            return NSImage(systemSymbolName: paused ? "eye.slash" : "eye", accessibilityDescription: "Reigns")!
        }
        let size = NSSize(width: (source.size.width / max(source.size.height, 1) * height).rounded(), height: height)
        let icon = NSImage(size: size, flipped: false) { rect in
            source.draw(in: rect, from: .zero, operation: .sourceOver, fraction: paused ? 0.35 : 1)
            return true
        }
        icon.isTemplate = true
        icon.accessibilityDescription = paused ? "Reigns (paused)" : "Reigns"
        return icon
    }
}
