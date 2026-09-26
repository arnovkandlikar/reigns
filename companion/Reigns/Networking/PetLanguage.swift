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

    /// Value for session.start / session.update "language". Sent explicitly (also "en"), since
    /// session.update ignores a missing field, so switching back to English needs "en".
    var wireCode: String {
        switch self {
        case .english: return "en"
        case .spanish: return "es"
        }
    }

    private static let defaultsKey = "petLanguage"

    static var saved: PetLanguage {
        get { UserDefaults.standard.string(forKey: defaultsKey).flatMap(PetLanguage.init) ?? .english }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }
}
