#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
app_path="${1:-$project_root/desktop/macos/dist/SoloScale AI OS.app}"
output_root="${2:-$project_root/desktop/macos/release}"
codesign_identity="${SOLOSCALE_CODESIGN_IDENTITY:-}"
notary_profile="${SOLOSCALE_NOTARY_PROFILE:-}"
allow_unsigned="${SOLOSCALE_ALLOW_UNSIGNED_DMG:-0}"
fail() { echo "macOS release: $*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "must run on macOS"
[[ -d "$app_path" && -x "$app_path/Contents/MacOS/SoloScaleDesktop" ]] || fail "app bundle is missing: $app_path"
command -v hdiutil >/dev/null || fail "hdiutil is required"
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app_path/Contents/Info.plist")"
architecture="$(uname -m)"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || fail "app has an invalid version"
[[ "$architecture" == "arm64" || "$architecture" == "x86_64" ]] || fail "unsupported architecture: $architecture"

if [[ -z "$codesign_identity" && "$allow_unsigned" != "1" ]]; then
  fail "SOLOSCALE_CODESIGN_IDENTITY is required; set SOLOSCALE_ALLOW_UNSIGNED_DMG=1 only for local smoke tests"
fi
if [[ -n "$codesign_identity" && -z "$notary_profile" ]]; then
  fail "SOLOSCALE_NOTARY_PROFILE is required for a signed public release"
fi

dmg_path="$output_root/SoloScale-AI-OS-$version-$architecture.dmg"
checksum_path="$dmg_path.sha256"
[[ ! -e "$dmg_path" && ! -e "$checksum_path" ]] || fail "release output already exists"
mkdir -p "$output_root"
stage_root="$(mktemp -d "$output_root/.dmg-stage.XXXXXX")"
trap 'rm -rf "$stage_root"' EXIT
/usr/bin/ditto "$app_path" "$stage_root/SoloScale AI OS.app"
/bin/ln -s /Applications "$stage_root/Applications"

if [[ -n "$codesign_identity" ]]; then
  /usr/bin/codesign --verify --deep --strict --verbose=2 "$stage_root/SoloScale AI OS.app"
fi
/usr/bin/hdiutil create -quiet -fs HFS+ -format UDZO -volname "SoloScale AI OS" -srcfolder "$stage_root" "$dmg_path"

if [[ -n "$codesign_identity" ]]; then
  /usr/bin/codesign --force --timestamp --sign "$codesign_identity" "$dmg_path"
  /usr/bin/codesign --verify --strict --verbose=2 "$dmg_path"
  /usr/bin/xcrun notarytool submit "$dmg_path" --keychain-profile "$notary_profile" --wait
  /usr/bin/xcrun stapler staple "$dmg_path"
  /usr/bin/xcrun stapler validate "$dmg_path"
  /usr/sbin/spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg_path"
fi

/usr/bin/shasum -a 256 "$dmg_path" > "$checksum_path"
echo "$dmg_path"
echo "$checksum_path"
