import os

/// PRD §14 rule 8: os.Logger only, no print in committed code.
enum Log {
    static let app = Logger(subsystem: "app.reigns", category: "app")
    static let ax = Logger(subsystem: "app.reigns", category: "ax")
    static let pet = Logger(subsystem: "app.reigns", category: "pet")
    static let net = Logger(subsystem: "app.reigns", category: "net")
}
