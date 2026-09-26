import Foundation

/// FR-A2: which side of Claude's bottom edge the pet peeks from; user-draggable and remembered.
enum PetSide: String {
    case left, right

    private static let defaultsKey = "petSide"

    static var saved: PetSide {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(PetSide.init) ?? .right }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }

    /// Panel x (Cocoa coordinates) for a panel of `width` on this side of `window`.
    func x(for width: CGFloat, in window: CGRect, inset: CGFloat) -> CGFloat {
        switch self {
        case .left: return window.minX + inset
        case .right: return window.maxX - inset - width
        }
    }

    static func nearest(toX x: CGFloat, in window: CGRect) -> PetSide {
        x < window.midX ? .left : .right
    }
}
