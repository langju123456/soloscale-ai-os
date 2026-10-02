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

Both the backend sidecar and the App build fail unless the source worktree is clean
and fully committed. The backend emits `source-provenance.json` with its full source
commit and clean marker. The App build accepts the sidecar only when that receipt
matches the App source commit, and the validation launcher independently verifies the
embedded receipt before launch. Missing, abbreviated, dirty, mismatched, malformed,
or symlinked provenance is rejected.

Launch a developer validation bundle through the deterministic process gate:

```bash
./scripts/launch_macos_validation_app.py \
  "desktop/macos/dist/SoloScale AI OS Dev.app"
```

Do not use `open -n` for validation. The launcher first terminates only verified
SoloScale Desktop app/backend processes, confirms that none remain, opens the exact
bundle path once, and records its embedded provenance.

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
