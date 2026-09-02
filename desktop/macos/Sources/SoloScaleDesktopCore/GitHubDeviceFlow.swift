import Foundation

public func validatedGitHubAppClientID(_ raw: String) -> String? {
    let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard value == raw,
          (10...100).contains(value.count),
          value.first != ".",
          value.last != ".",
          value.unicodeScalars.allSatisfy({
              CharacterSet.alphanumerics.contains($0) || $0 == "."
          })
    else { return nil }
    return value
}

public enum GitHubDeviceFlowError: LocalizedError {
    case notConfigured
    case invalidResponse
    case authorizationDenied
    case authorizationExpired
    case networkFailure

    public var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "GitHub Connect is not configured in this build."
        case .invalidResponse:
            return "GitHub returned an invalid authorization response."
        case .authorizationDenied:
            return "GitHub authorization was cancelled or denied."
        case .authorizationExpired:
            return "The GitHub authorization code expired. Please try again."
        case .networkFailure:
            return "GitHub authorization could not be reached."
        }
    }
}

public struct GitHubDeviceAuthorization {
    public let deviceCode: String
    public let userCode: String
    public let verificationURL: URL
    public let expiresAt: Date
    public let interval: TimeInterval
}

public final class GitHubDeviceFlowClient {
    private let session: URLSession

    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 30
        session = URLSession(configuration: configuration)
    }

    public func requestAuthorization(
        clientID: String,
        completion: @escaping (Result<GitHubDeviceAuthorization, Error>) -> Void
    ) {
        guard !clientID.isEmpty, clientID == clientID.trimmingCharacters(in: .whitespacesAndNewlines) else {
            completion(.failure(GitHubDeviceFlowError.notConfigured))
            return
        }
        guard let url = URL(string: "https://github.com/login/device/code") else {
            completion(.failure(GitHubDeviceFlowError.invalidResponse))
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(
            "application/x-www-form-urlencoded",
            forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = formBody([
            "client_id": clientID,
        ])
        session.dataTask(with: request) { data, response, error in
            guard error == nil,
                  let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let deviceCode = payload["device_code"] as? String,
                  let userCode = payload["user_code"] as? String,
                  let verification = payload["verification_uri"] as? String,
                  let verificationURL = URL(string: verification),
                  verificationURL.scheme == "https",
                  verificationURL.host == "github.com",
                  let expiresIn = payload["expires_in"] as? NSNumber,
                  let interval = payload["interval"] as? NSNumber,
                  !deviceCode.isEmpty,
                  !userCode.isEmpty
            else {
                completion(.failure(
                    error == nil
                        ? GitHubDeviceFlowError.invalidResponse
                        : GitHubDeviceFlowError.networkFailure
                ))
                return
            }
            completion(.success(GitHubDeviceAuthorization(
                deviceCode: deviceCode,
                userCode: userCode,
                verificationURL: verificationURL,
                expiresAt: Date().addingTimeInterval(expiresIn.doubleValue),
                interval: max(5, interval.doubleValue)
            )))
        }.resume()
    }

    public func pollForToken(
        clientID: String,
        authorization: GitHubDeviceAuthorization,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        poll(
            clientID: clientID,
            authorization: authorization,
            interval: authorization.interval,
            completion: completion
        )
    }

    private func poll(
        clientID: String,
        authorization: GitHubDeviceAuthorization,
        interval: TimeInterval,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard Date() < authorization.expiresAt else {
            completion(.failure(GitHubDeviceFlowError.authorizationExpired))
            return
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + interval) {
            guard let url = URL(string: "https://github.com/login/oauth/access_token") else {
                completion(.failure(GitHubDeviceFlowError.invalidResponse))
                return
            }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            request.setValue(
                "application/x-www-form-urlencoded",
                forHTTPHeaderField: "Content-Type"
            )
            request.httpBody = formBody([
                "client_id": clientID,
                "device_code": authorization.deviceCode,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            ])
            self.session.dataTask(with: request) { data, response, error in
                guard error == nil,
                      let http = response as? HTTPURLResponse,
                      http.statusCode == 200,
                      let data,
                      let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else {
                    completion(.failure(GitHubDeviceFlowError.networkFailure))
                    return
                }
                if let token = payload["access_token"] as? String,
                   !token.isEmpty,
                   token == token.trimmingCharacters(in: .whitespacesAndNewlines) {
                    completion(.success(token))
                    return
                }
                switch payload["error"] as? String {
                case "authorization_pending":
                    self.poll(
                        clientID: clientID,
                        authorization: authorization,
                        interval: interval,
                        completion: completion
                    )
                case "slow_down":
                    self.poll(
                        clientID: clientID,
                        authorization: authorization,
                        interval: interval + 5,
                        completion: completion
                    )
                case "expired_token":
                    completion(.failure(GitHubDeviceFlowError.authorizationExpired))
                case "access_denied":
                    completion(.failure(GitHubDeviceFlowError.authorizationDenied))
                default:
                    completion(.failure(GitHubDeviceFlowError.invalidResponse))
                }
            }.resume()
        }
    }
}

private func formBody(_ values: [String: String]) -> Data? {
    var components = URLComponents()
    components.queryItems = values.map { URLQueryItem(name: $0.key, value: $0.value) }
    return components.percentEncodedQuery?.data(using: .utf8)
}
