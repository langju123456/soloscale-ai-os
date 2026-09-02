#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_path="${SOLOSCALE_TOOLCHAIN_CONFIG:-$project_root/desktop/macos/toolchain.env}"
fail() { echo "SoloScale Desktop Build Preflight: FAIL - $*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required"
[[ -f "$config_path" ]] || fail "toolchain config is missing: $config_path"

# shellcheck disable=SC1090
source "$config_path"

: "${SOLOSCALE_TOOLCHAIN_KIND:?missing SOLOSCALE_TOOLCHAIN_KIND}"
: "${SOLOSCALE_DEVELOPER_DIR:?missing SOLOSCALE_DEVELOPER_DIR}"
: "${SOLOSCALE_EXPECTED_XCODE_VERSION:?missing SOLOSCALE_EXPECTED_XCODE_VERSION}"

[[ -d "$SOLOSCALE_DEVELOPER_DIR" ]] || fail "developer directory is unavailable: $SOLOSCALE_DEVELOPER_DIR"
[[ "$SOLOSCALE_TOOLCHAIN_KIND" == "full-xcode" ]] || fail "only the canonical full-Xcode toolchain is supported"
[[ "$SOLOSCALE_DEVELOPER_DIR" == */Contents/Developer ]] || fail "full-xcode must point to Xcode.app/Contents/Developer"

export DEVELOPER_DIR="$SOLOSCALE_DEVELOPER_DIR"
unset SDKROOT

swift_path="$(/usr/bin/xcrun --find swift 2>/dev/null)" || fail "Swift is unavailable from the pinned developer directory"
clang_path="$(/usr/bin/xcrun --find clang 2>/dev/null)" || fail "clang is unavailable from the pinned developer directory"
sdk_root="$(/usr/bin/xcrun --sdk macosx --show-sdk-path 2>/dev/null)" || fail "macOS SDK is unavailable from the pinned developer directory"
for tool_path in "$swift_path" "$clang_path" "$sdk_root"; do
  case "$tool_path" in
    "$SOLOSCALE_DEVELOPER_DIR"/*) ;;
    *) fail "mixed toolchain: $tool_path is outside the canonical developer directory" ;;
  esac
done
swift_version_output="$("$swift_path" --version 2>&1)" || fail "Swift version inspection failed"
swift_version="$(printf '%s\n' "$swift_version_output" | /usr/bin/sed -nE 's/.*Apple Swift version ([^ ]+).*/\1/p' | /usr/bin/head -1)"
sdk_version="$(/usr/bin/xcrun --sdk macosx --show-sdk-version 2>/dev/null)" || fail "macOS SDK version inspection failed"
xcode_version_output="$(/usr/bin/xcodebuild -version 2>/dev/null)" || fail "full Xcode is unavailable"
xcode_version="$(printf '%s\n' "$xcode_version_output" | /usr/bin/sed -nE 's/^Xcode (.+)$/\1/p')"
xcode_build="$(printf '%s\n' "$xcode_version_output" | /usr/bin/sed -nE 's/^Build version (.+)$/\1/p')"

[[ "$xcode_version" == "$SOLOSCALE_EXPECTED_XCODE_VERSION" ]] || fail "Xcode drifted: expected $SOLOSCALE_EXPECTED_XCODE_VERSION, found ${xcode_version:-unknown}"

cat <<EOF
SoloScale Desktop Build Preflight

Developer:
 $SOLOSCALE_DEVELOPER_DIR

Toolchain kind:
 $SOLOSCALE_TOOLCHAIN_KIND

Xcode:
 $xcode_version (build $xcode_build)

Swift:
 $swift_version

Swift executable:
 $swift_path

Clang executable:
 $clang_path

macOS SDK:
 $sdk_version ($sdk_root)

Status:
 PASS
EOF
