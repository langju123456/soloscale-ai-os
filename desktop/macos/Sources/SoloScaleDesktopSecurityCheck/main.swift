import Foundation
import SoloScaleDesktopCore

let token = String(repeating: "0123456789abcdef", count: 4)
let nonce = String(repeating: "abcdef0123456789", count: 4)
let origin = URL(string: "http://127.0.0.1:54321")!
let receipt = """
{"schema_version":"1.0","url":"http://127.0.0.1:54321","pid":42,"proof":"bbc58f3626bd3318a2adfb434f5e4c4aaff8ebc73188e74232d53b2f2a197fec"}
"""
guard let data = receipt.data(using: .utf8) else { fatalError("receipt encoding failed") }
guard authenticatedReadinessURL(data: data, token: token, launchedPID: 42)?.absoluteString
        == "http://127.0.0.1:54321"
else { fatalError("valid authenticated receipt was rejected") }
guard authenticatedReadinessURL(data: data, token: token, launchedPID: 41) == nil else {
    fatalError("receipt was not bound to the launched process")
}
let tampered = data.replacingOccurrences(of: Data("54321".utf8), with: Data("54322".utf8))
guard authenticatedReadinessURL(data: tampered, token: token, launchedPID: 42) == nil else {
    fatalError("tampered readiness URL was accepted")
}

let requestProof = desktopBootstrapRequestProof(
    token: token,
    origin: origin,
    pid: 42,
    nonce: nonce
)
guard requestProof == "09f6acc861604d3453c27bb4ca78212b1ebe6e55c7681d24286f7360bf7a7dd8"
else { fatalError("bootstrap request vector differs from Python") }
let cookie = desktopSessionCookie(token: token, origin: origin, pid: 42, nonce: nonce)
guard cookie == "v1_47f491dbd10b8d90fe540f73151426b5ac29b938485f9214da1947169c48e031"
else { fatalError("session cookie vector differs from Python") }
let responseProof = desktopBootstrapResponseProof(
    token: token,
    origin: origin,
    pid: 42,
    nonce: nonce,
    cookie: cookie!
)
guard responseProof == "4b437d02ba41329cd1a7c808d837f0e007220fa17bd92dfaa28cad35665a1cb7"
else { fatalError("bootstrap response vector differs from Python") }
guard isValidDesktopBootstrapResponse(
    token: token,
    origin: origin,
    pid: 42,
    nonce: nonce,
    status: 200,
    responseNonce: nonce,
    responseProof: responseProof,
    cookie: cookie!
) else { fatalError("valid bootstrap response was rejected") }
guard !isValidDesktopBootstrapResponse(
    token: token,
    origin: origin,
    pid: 42,
    nonce: nonce,
    status: 302,
    responseNonce: nonce,
    responseProof: responseProof,
    cookie: cookie!
) else { fatalError("bootstrap redirect was accepted") }
guard !isValidDesktopBootstrapResponse(
    token: token,
    origin: origin,
    pid: 42,
    nonce: nonce,
    status: 200,
    responseNonce: String(repeating: "0", count: 64),
    responseProof: responseProof,
    cookie: cookie!
) else { fatalError("wrong bootstrap nonce was accepted") }
guard try desktopCredentialFrame(nil) == Data([0, 0, 0, 0]) else {
    fatalError("unconfigured credential frame was not zero-length")
}
guard try desktopCredentialFrame(Data([0x61, 0x62, 0x63])) == Data([0, 0, 0, 3, 0x61, 0x62, 0x63]) else {
    fatalError("credential frame is not big-endian")
}
do {
    _ = try desktopCredentialFrame(Data(repeating: 1, count: desktopCredentialFrameMaximum + 1))
    fatalError("oversized credential frame was accepted")
} catch DesktopAICredentialError.frameTooLarge {
    // Expected: the sidecar input frame is bounded before process startup.
}
for invalidKey in [" leading", "trailing ", "\nline-break"] {
    do {
        _ = try desktopAIKeyPayload(invalidKey)
        fatalError("credential key with leading or trailing whitespace was accepted")
    } catch DesktopAICredentialError.invalidKey {
        // Expected: Swift and Python reject the same unsafe input before startup.
    }
}
guard validatedGitHubAppClientID("Iv1.0123456789abcdef") != nil else {
    fatalError("valid GitHub App client ID was rejected")
}
for invalidClientID in [" Iv1.0123456789abcdef", "Iv1.0123456789abcdef ", ".Iv10123456789"] {
    guard validatedGitHubAppClientID(invalidClientID) == nil else {
        fatalError("invalid GitHub App client ID was accepted")
    }
}
print("authenticated readiness and bootstrap checks passed")

private extension Data {
    func replacingOccurrences(of target: Data, with replacement: Data) -> Data {
        guard let range = range(of: target) else { return self }
        var copy = self
        copy.replaceSubrange(range, with: replacement)
        return copy
    }
}
