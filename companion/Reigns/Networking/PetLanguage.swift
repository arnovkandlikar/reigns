import Foundation

/// Language the pet speaks (engine voice and bubble copy). Menu › Language.
enum PetLanguage: String, CaseIterable, Identifiable {
    case english, spanish

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .english: return "English"
        case .spanish: return "Español"
        }
    }

    /// Value for session.start "language". English is the engine's default, so it's left out.
    var wireCode: String? {
        switch self {
        case .english: return nil
        case .spanish: return "es"
        }
    }

    private static let defaultsKey = "petLanguage"

    static var saved: PetLanguage {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(PetLanguage.init) ?? .english }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }
}
