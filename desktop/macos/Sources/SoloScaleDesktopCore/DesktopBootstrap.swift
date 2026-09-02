import CryptoKit
import Foundation

public let desktopBootstrapPath = "/__desktop/bootstrap"
public let desktopNonceHeader = "X-SoloScale-Bootstrap-Nonce"
public let desktopProofHeader = "X-SoloScale-Bootstrap-Proof"
public let desktopCookieName = "soloscale_desktop_session"

public func desktopBootstrapRequestProof(
    token: String,
    origin: URL,
    pid: Int32,
    nonce: String
) -> String? {
    guard isDesktopSecret(token), isDesktopSecret(nonce), isExactLoopbackURL(origin) else {
        return nil
    }
    let payload = """
    soloscale.desktop.bootstrap.request.v1
    POST
    \(desktopBootstrapPath)
    \(origin.absoluteString)
    \(pid)
    \(nonce)
    """
    return desktopHMAC(token: token, payload: payload)
}

public func desktopSessionCookie(
    token: String,
    origin: URL,
    pid: Int32,
    nonce: String
) -> String? {
    guard isDesktopSecret(token), isDesktopSecret(nonce), isExactLoopbackURL(origin) else {
        return nil
    }
    let payload = """
    soloscale.desktop.session-cookie.v1
    \(origin.absoluteString)
    \(pid)
    \(nonce)
    """
    return "v1_\(desktopHMAC(token: token, payload: payload))"
}

public func desktopBootstrapResponseProof(
    token: String,
    origin: URL,
    pid: Int32,
    nonce: String,
    cookie: String
) -> String? {
    guard isDesktopSecret(token), isDesktopSecret(nonce), isExactLoopbackURL(origin) else {
        return nil
    }
    let cookieHash = SHA256.hash(data: Data(cookie.utf8))
        .map { String(format: "%02x", $0) }
        .joined()
    let payload = """
    soloscale.desktop.bootstrap.response.v1
    POST
    \(desktopBootstrapPath)
    200
    \(origin.absoluteString)
    \(pid)
    \(nonce)
    \(cookieHash)
    """
    return desktopHMAC(token: token, payload: payload)
}

public func isValidDesktopBootstrapResponse(
    token: String,
    origin: URL,
    pid: Int32,
    nonce: String,
    status: Int,
    responseNonce: String?,
    responseProof: String?,
    cookie: String
) -> Bool {
    guard
        status == 200,
        responseNonce == nonce,
        let responseProof,
        let expectedCookie = desktopSessionCookie(
            token: token,
            origin: origin,
            pid: pid,
            nonce: nonce
        ),
        cookie == expectedCookie,
        let expectedProof = desktopBootstrapResponseProof(
            token: token,
            origin: origin,
            pid: pid,
            nonce: nonce,
            cookie: cookie
        )
    else { return false }
    return constantTimeEqual(responseProof, expectedProof)
}

public func randomDesktopSecret() -> String {
    var generator = SystemRandomNumberGenerator()
    return (0..<32).map { _ in
        String(format: "%02x", UInt8.random(in: .min ... .max, using: &generator))
    }.joined()
}

private func desktopHMAC(token: String, payload: String) -> String {
    let key = SymmetricKey(data: Data(token.utf8))
    return HMAC<SHA256>.authenticationCode(
        for: Data(payload.utf8),
        using: key
    ).map { String(format: "%02x", $0) }.joined()
}

private func isDesktopSecret(_ value: String) -> Bool {
    value.count == 64 && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
}

private func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
    guard lhs.utf8.count == rhs.utf8.count else { return false }
    var difference: UInt8 = 0
    for (left, right) in zip(lhs.utf8, rhs.utf8) {
        difference |= left ^ right
    }
    return difference == 0
}
