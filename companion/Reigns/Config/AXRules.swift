import Foundation

/// Element-matching rules and tunables loaded from AXRules.json (PRD R8: Claude app updates
/// should only need a config change, not a code change).
struct AXRules: Decodable {
    var claudeBundleID: String
    var petInsetPx: Double
    var windowPollMs: Int
    /// FR-A9: Claude's message box is the AXTextArea with this DOM class.
    var composerDOMClass: String
    var conversation: Conversation

    /// FR-A3: how messages are found in Claude's AX tree (see ConversationReader).
    struct Conversation: Decodable {
        var userHeadingPrefix: String
        var assistantHeadingPrefix: String
        /// Title prefix of the per-message AXDocumentArticle ("Message N"), used for positions.
        var articleTitlePrefix: String
        /// With a single message, how far above its heading the message list container sits.
        var singleMessageContainerLevelsUp: Int
        /// Subtrees never read as message text (message action buttons, tool pills, the composer).
        var skipRoles: Set<String>
        /// Subtrees skipped by subrole (e.g. AXApplicationStatus screen-reader status duplicates).
        var skipSubroles: Set<String>
        /// Widget containers inside replies, matched on DOM id or class (visuals, tool-step
        /// summaries, attached-file cards).
        var skipDOMIDPrefixes: [String]
        var skipDOMIDSuffixes: [String]
        var skipDOMClasses: Set<String>
        /// Claude's mode switch (Chat / Code): when a mode with one of these labels is selected, we
        /// don't read the window at all.
        var ignoredModeTitles: Set<String>
        /// Button titles that only exist while Claude is generating (FR-A4 backup signal).
        var respondingButtonTitles: Set<String>
        /// Text runs that are app chrome, not message content (e.g. the feedback survey prompt).
        var ignoredTexts: [String]

        private enum CodingKeys: String, CodingKey {
            case userHeadingPrefix = "user_heading_prefix"
            case assistantHeadingPrefix = "assistant_heading_prefix"
            case articleTitlePrefix = "article_title_prefix"
            case singleMessageContainerLevelsUp = "single_message_container_levels_up"
            case skipRoles = "skip_roles"
            case ignoredTexts = "ignored_texts"
            case respondingButtonTitles = "responding_button_titles"
            case ignoredModeTitles = "ignored_mode_titles"
            case skipSubroles = "skip_subroles"
            case skipDOMIDPrefixes = "skip_dom_id_prefixes"
            case skipDOMIDSuffixes = "skip_dom_id_suffixes"
            case skipDOMClasses = "skip_dom_classes"
        }
    }

    private enum CodingKeys: String, CodingKey {
        case claudeBundleID = "claude_bundle_id"
        case petInsetPx = "pet_inset_px"
        case windowPollMs = "window_poll_ms"
        case composerDOMClass = "composer_dom_class"
        case conversation
    }

    static let fallback = AXRules(
        claudeBundleID: "com.anthropic.claudefordesktop",
        petInsetPx: 24,
        windowPollMs: 250,
        composerDOMClass: "ProseMirror",
        conversation: Conversation(
            userHeadingPrefix: "You said: ",
            assistantHeadingPrefix: "Claude responded: ",
            articleTitlePrefix: "Message ",
            singleMessageContainerLevelsUp: 4,
            skipRoles: ["AXButton", "AXToolbar", "AXTextArea", "AXTextField", "AXPopUpButton", "AXCheckBox", "AXMenu"],
            skipSubroles: ["AXApplicationStatus"],
            skipDOMIDPrefixes: ["mcp-app-"],
            skipDOMIDSuffixes: ["-label"],
            skipDOMClasses: ["group/artifact-block"],
            ignoredModeTitles: ["Code"],
            respondingButtonTitles: ["Stop response", "Stop generating", "Stop"],
            ignoredTexts: ["How is Claude doing this session?"]))

    static func load() -> AXRules {
        guard let url = Bundle.main.url(forResource: "AXRules", withExtension: "json"),
              let data = try? Data(contentsOf: url)
        else {
            Log.app.error("AXRules.json missing from bundle; using fallback rules")
            return .fallback
        }
        do {
            return try JSONDecoder().decode(AXRules.self, from: data)
        } catch {
            Log.app.error("AXRules.json invalid (\(String(describing: error), privacy: .public)); using fallback rules")
            return .fallback
        }
    }
}
