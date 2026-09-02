# SoloScale macOS Desktop toolchain

Desktop builds do not use the ambient `xcode-select` or `DEVELOPER_DIR` state. The
canonical toolchain is declared in `desktop/macos/toolchain.env` and verified by
`scripts/check_macos_toolchain.sh` before Swift compilation starts.

The supported build path is one complete Xcode installation. Swift, clang, and the
macOS SDK are resolved through the same canonical `DEVELOPER_DIR`; Command Line Tools
and manually pinned SDK paths are not fallback build paths.

Run the preflight directly:

```bash
./scripts/check_macos_toolchain.sh
```

Build the app through the normal command:

```bash
./scripts/build_macos_app.sh
```

The build script loads the same config, overrides ambient `DEVELOPER_DIR` and
`SDKROOT`, then resolves Swift and the current macOS SDK through Xcode's `xcrun`. The
preflight fails before compilation if Xcode drifts or any tool resolves outside that
developer directory.

The canonical configuration is:

```text
SOLOSCALE_TOOLCHAIN_KIND="full-xcode"
SOLOSCALE_DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
SOLOSCALE_EXPECTED_XCODE_VERSION="26.6"
```

When intentionally upgrading Xcode, update this one expected Xcode version. Do not add
a Command Line Tools or manual-SDK fallback.
