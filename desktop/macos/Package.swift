// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SoloScaleDesktop",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "SoloScaleDesktop", targets: ["SoloScaleDesktop"])],
    targets: [
        .target(name: "SoloScaleDesktopCore"),
        .executableTarget(name: "SoloScaleDesktop", dependencies: ["SoloScaleDesktopCore"]),
        .executableTarget(
            name: "SoloScaleDesktopSecurityCheck",
            dependencies: ["SoloScaleDesktopCore"]
        ),
    ]
)
