import Foundation

/// Which pet is on screen. Both share every expression, level colour and accessory. Marley is a
/// unicorn (horn, rainbow mane, sparkles) with her own engine voice and personality.
enum PetCharacter: String, CaseIterable, Identifiable {
    case charlie, marley

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .charlie: return "Charlie"
        case .marley: return "Marley"
        }
    }

    /// Value for session.start "character" (picks the engine voice and personality).
    var wireCode: String {
        rawValue  // "charlie" or "marley"
    }

    private static let defaultsKey = "petCharacter"

    static var saved: PetCharacter {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(PetCharacter.init) ?? .charlie }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }
}
