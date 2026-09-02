import Foundation
import LocalAuthentication
import Security

public enum DesktopAICredentialError: LocalizedError {
    case invalidKey
    case keychain(OSStatus)
    case frameTooLarge

    public var errorDescription: String? {
        switch self {
        case .invalidKey:
            return "The API key is not valid."
        case .keychain:
            return "The API key could not be stored securely in Keychain."
        case .frameTooLarge:
            return "The saved API key is too large for the local service."
        }
    }
}

public enum DesktopOpenAIKeychain {
    public static let service = "local.soloscale.desktop.ai.openai"
    public static let account = "default"

    public static func save(_ apiKey: String) throws {
        try DesktopSecretKeychain.save(
            try desktopAIKeyPayload(apiKey),
            service: service,
            account: account
        )
    }

    public static func delete() throws {
        try DesktopSecretKeychain.delete(service: service, account: account)
    }

    public static func read() throws -> Data? {
        try DesktopSecretKeychain.read(service: service, account: account)
    }
}

public enum DesktopGitHubKeychain {
    public static let service = "local.soloscale.desktop.github"
    public static let account = "default"

    public static func save(_ accessToken: String) throws {
        try DesktopSecretKeychain.save(
            try desktopAIKeyPayload(accessToken),
            service: service,
            account: account
        )
    }

    public static func delete() throws {
        try DesktopSecretKeychain.delete(service: service, account: account)
    }

    public static func read() throws -> Data? {
        try DesktopSecretKeychain.read(service: service, account: account)
    }
}

public enum DesktopHeyGenKeychain {
    public static let service = "local.soloscale.desktop.media.heygen"
    public static let account = "default"

    public static func save(_ apiKey: String) throws {
        try DesktopSecretKeychain.save(
            try desktopAIKeyPayload(apiKey),
            service: service,
            account: account
        )
    }

    public static func delete() throws {
        try DesktopSecretKeychain.delete(service: service, account: account)
    }

    public static func read() throws -> Data? {
        try DesktopSecretKeychain.read(service: service, account: account)
    }
}

private enum DesktopSecretKeychain {
    static func save(_ data: Data, service: String, account: String) throws {
        var query = baseQuery(service: service, account: account)
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        query[kSecAttrSynchronizable as String] = false
        let addStatus = SecItemAdd(query as CFDictionary, nil)
        if addStatus == errSecSuccess { return }
        guard addStatus == errSecDuplicateItem else {
            throw DesktopAICredentialError.keychain(addStatus)
        }
        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecAttrSynchronizable as String: false,
        ]
        let updateStatus = SecItemUpdate(
            baseQuery(service: service, account: account) as CFDictionary,
            update as CFDictionary
        )
        guard updateStatus == errSecSuccess else {
            throw DesktopAICredentialError.keychain(updateStatus)
        }
    }

    static func delete(service: String, account: String) throws {
        let status = SecItemDelete(
            baseQuery(service: service, account: account) as CFDictionary
        )
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw DesktopAICredentialError.keychain(status)
        }
    }

    static func read(service: String, account: String) throws -> Data? {
        var query = baseQuery(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        // Startup must never wait behind an invisible Keychain authorization dialog.
        // A credential that needs interaction remains unavailable until the user
        // reconnects it from the visible settings flow.
        let authenticationContext = LAContext()
        authenticationContext.interactionNotAllowed = true
        query[kSecUseAuthenticationContext as String] = authenticationContext
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound || status == errSecInteractionNotAllowed {
            return nil
        }
        guard status == errSecSuccess, let data = item as? Data else {
            throw DesktopAICredentialError.keychain(status)
        }
        guard !data.isEmpty, data.count <= desktopCredentialFrameMaximum else {
            throw DesktopAICredentialError.frameTooLarge
        }
        return data
    }

    private static func baseQuery(service: String, account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false,
        ]
    }
}

public let desktopCredentialFrameMaximum = 512
public let desktopCredentialEnvelopeMaximum = 4096

/// Validates the exact API-key bytes that will be sent to the Python sidecar.
/// Whitespace is rejected rather than trimmed so a saved key cannot differ from
/// the configured key the operator intended to use.
public func desktopAIKeyPayload(_ apiKey: String) throws -> Data {
    guard apiKey == apiKey.trimmingCharacters(in: .whitespacesAndNewlines),
          let data = apiKey.data(using: .utf8),
          !data.isEmpty,
          data.count <= desktopCredentialFrameMaximum
    else {
        throw DesktopAICredentialError.invalidKey
    }
    return data
}

/// Four-byte big-endian payload length followed by opaque credential bytes.
/// A zero length frame explicitly means that no credential is configured.
public func desktopCredentialFrame(_ payload: Data?) throws -> Data {
    let body = payload ?? Data()
    guard body.count <= desktopCredentialFrameMaximum else {
        throw DesktopAICredentialError.frameTooLarge
    }
    var length = UInt32(body.count).bigEndian
    var frame = Data(bytes: &length, count: MemoryLayout<UInt32>.size)
    frame.append(body)
    return frame
}

/// One framed JSON envelope keeps multiple Keychain secrets off process arguments,
/// environment variables, settings files, pages, and logs.
public func desktopCredentialEnvelopeFrame(
    openAIKey: Data?,
    githubAccessToken: Data?,
    heygenAPIKey: Data?
) throws -> Data {
    var envelope: [String: Any] = ["schema_version": "1.0"]
    if let openAIKey {
        guard let value = String(data: openAIKey, encoding: .utf8) else {
            throw DesktopAICredentialError.invalidKey
        }
        envelope["openai_api_key"] = value
    }
    if let githubAccessToken {
        guard let value = String(data: githubAccessToken, encoding: .utf8) else {
            throw DesktopAICredentialError.invalidKey
        }
        envelope["github_access_token"] = value
    }
    if let heygenAPIKey {
        guard let value = String(data: heygenAPIKey, encoding: .utf8) else {
            throw DesktopAICredentialError.invalidKey
        }
        envelope["heygen_api_key"] = value
    }
    let body = try JSONSerialization.data(
        withJSONObject: envelope,
        options: [.sortedKeys]
    )
    guard body.count <= desktopCredentialEnvelopeMaximum else {
        throw DesktopAICredentialError.frameTooLarge
    }
    var length = UInt32(body.count).bigEndian
    var frame = Data(bytes: &length, count: MemoryLayout<UInt32>.size)
    frame.append(body)
    return frame
}
