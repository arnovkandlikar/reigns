import Foundation

/// Which horse the pet is. Both share every expression, colour and accessory; Marley adds a
/// feminine look (eyelashes, flowing mane, bow, rosy cheeks). Same voice for now.
enum PetCharacter: String, CaseIterable, Identifiable {
    case charlie, marley

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .charlie: return "Charlie"
        case .marley: return "Marley"
        }
    }

    private static let defaultsKey = "petCharacter"

    static var saved: PetCharacter {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(PetCharacter.init) ?? .charlie }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }
}
