import CryptoKit
import Foundation

public struct DesktopReadinessReceipt: Decodable {
    public let schema_version: String
    public let url: String
    public let pid: Int32
    public let proof: String
}

public func authenticatedReadinessURL(
    data: Data,
    token: String,
    launchedPID: Int32
) -> URL? {
    guard
        let receipt = try? JSONDecoder().decode(DesktopReadinessReceipt.self, from: data),
        receipt.schema_version == "1.0",
        receipt.pid == launchedPID,
        let url = URL(string: receipt.url),
        isExactLoopbackURL(url),
        let authenticationCode = decodeHex(receipt.proof)
    else { return nil }

    let payload = Data("1.0\n\(receipt.url)\n\(receipt.pid)".utf8)
    let key = SymmetricKey(data: Data(token.utf8))
    guard HMAC<SHA256>.isValidAuthenticationCode(
        authenticationCode,
        authenticating: payload,
        using: key
    ) else { return nil }
    return url
}

public func isExactLoopbackURL(_ url: URL) -> Bool {
    guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
        return false
    }
    return components.scheme == "http"
        && components.host == "127.0.0.1"
        && components.port != nil
        && (components.path.isEmpty || components.path == "/")
        && components.query == nil
        && components.fragment == nil
        && components.user == nil
        && components.password == nil
}

private func decodeHex(_ value: String) -> Data? {
    guard value.count == 64 else { return nil }
    var result = Data(capacity: value.count / 2)
    var index = value.startIndex
    while index < value.endIndex {
        let next = value.index(index, offsetBy: 2)
        guard let byte = UInt8(value[index..<next], radix: 16) else { return nil }
        result.append(byte)
        index = next
    }
    return result
}
