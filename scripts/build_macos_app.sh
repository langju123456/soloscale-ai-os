#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
desktop_root="$project_root/desktop/macos"
toolchain_config="${SOLOSCALE_TOOLCHAIN_CONFIG:-$desktop_root/toolchain.env}"
output_root="${SOLOSCALE_APP_OUTPUT:-$project_root/desktop/macos/dist}"
app_root="$output_root/SoloScale AI OS.app"
sidecar_root="${SOLOSCALE_SIDECAR_ROOT:-$project_root/packaging/macos/dist/SoloScaleBackend}"
swift_scratch="${SOLOSCALE_SWIFT_SCRATCH:-$desktop_root/.build}"
bundle_identifier="${SOLOSCALE_BUNDLE_IDENTIFIER:-local.soloscale.desktop}"
version="${SOLOSCALE_VERSION:-0.4.1}"
build_number="${SOLOSCALE_BUILD_NUMBER:-6}"
codesign_identity="${SOLOSCALE_CODESIGN_IDENTITY:-}"
fail() { echo "macOS app build: $*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "must run on macOS"
[[ -f "$toolchain_config" ]] || fail "toolchain config is missing: $toolchain_config"
SOLOSCALE_TOOLCHAIN_CONFIG="$toolchain_config" "$project_root/scripts/check_macos_toolchain.sh"
# shellcheck disable=SC1090
source "$toolchain_config"
export DEVELOPER_DIR="$SOLOSCALE_DEVELOPER_DIR"
unset SDKROOT
export SDKROOT="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
swift_executable="$(/usr/bin/xcrun --find swift)"
[[ -f "$desktop_root/Package.swift" && -f "$desktop_root/Info.plist.template" ]] || fail "Swift app inputs are missing"
[[ -x "$sidecar_root/SoloScaleBackend" ]] || fail "backend sidecar is missing; run packaging/macos/build_backend_onedir.sh first"
[[ ! -e "$app_root" ]] || fail "output already exists: $app_root"
[[ "$bundle_identifier" =~ ^[A-Za-z0-9.-]+$ ]] || fail "invalid bundle identifier"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || fail "invalid version"
[[ "$build_number" =~ ^[0-9]+$ ]] || fail "invalid build number"
"$swift_executable" build --package-path "$desktop_root" --scratch-path "$swift_scratch" --configuration release
swift_bin_root="$("$swift_executable" build --package-path "$desktop_root" --scratch-path "$swift_scratch" --configuration release --show-bin-path)"
binary="$swift_bin_root/SoloScaleDesktop"
[[ -x "$binary" ]] || fail "Swift build did not create SoloScaleDesktop"
mkdir -p "$app_root/Contents/MacOS" "$app_root/Contents/Resources"
cp "$binary" "$app_root/Contents/MacOS/SoloScaleDesktop"
cp "$desktop_root/Info.plist.template" "$app_root/Contents/Info.plist"
/usr/bin/ditto "$sidecar_root" "$app_root/Contents/Resources/SoloScaleBackend"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $bundle_identifier" "$app_root/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$app_root/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $build_number" "$app_root/Contents/Info.plist"
if [[ -n "${SOLOSCALE_GITHUB_APP_CLIENT_ID:-}" ]]; then
  /usr/libexec/PlistBuddy -c "Set :SoloScaleGitHubAppClientID $SOLOSCALE_GITHUB_APP_CLIENT_ID" "$app_root/Contents/Info.plist"
fi
[[ -x "$app_root/Contents/Resources/SoloScaleBackend/SoloScaleBackend" ]] || fail "sidecar copy failed"
if [[ -n "$codesign_identity" ]]; then
  /usr/bin/codesign --force --options runtime --timestamp --sign "$codesign_identity" "$app_root/Contents/MacOS/SoloScaleDesktop"
  /usr/bin/codesign --force --options runtime --timestamp --sign "$codesign_identity" "$app_root"
else
  # Swift linker-signs the executable before the sidecar resources are copied.
  # Seal the completed local bundle so LaunchServices sees one valid app.
  /usr/bin/codesign --force --deep --sign - "$app_root"
fi
/usr/bin/codesign --verify --deep --strict --verbose=2 "$app_root"
echo "$app_root"
