#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
desktop_root="$project_root/desktop/macos"
toolchain_config="${SOLOSCALE_TOOLCHAIN_CONFIG:-$desktop_root/toolchain.env}"
output_root="${SOLOSCALE_APP_OUTPUT:-$project_root/desktop/macos/dist}"
sidecar_root="${SOLOSCALE_SIDECAR_ROOT:-$project_root/packaging/macos/dist/SoloScaleBackend}"
swift_scratch="${SOLOSCALE_SWIFT_SCRATCH:-$desktop_root/.build}"
build_kind="${SOLOSCALE_BUILD_KIND:-development}"
case "$build_kind" in
  development)
    default_bundle_identifier="local.soloscale.desktop.dev"
    default_display_name="SoloScale AI OS Dev"
    ;;
  production)
    default_bundle_identifier="local.soloscale.desktop"
    default_display_name="SoloScale AI OS"
    ;;
  *)
    echo "macOS app build: SOLOSCALE_BUILD_KIND must be development or production" >&2
    exit 1
    ;;
esac
bundle_identifier="${SOLOSCALE_BUNDLE_IDENTIFIER:-$default_bundle_identifier}"
display_name="${SOLOSCALE_DISPLAY_NAME:-$default_display_name}"
app_bundle_name="${SOLOSCALE_APP_BUNDLE_NAME:-$display_name}"
app_root="$output_root/$app_bundle_name.app"
version="${SOLOSCALE_VERSION:-0.4.1}"
build_number="${SOLOSCALE_BUILD_NUMBER:-6}"
codesign_identity="${SOLOSCALE_CODESIGN_IDENTITY:-}"
git_branch="unknown"
git_commit="unknown"
git_dirty="unknown"
if command -v git >/dev/null 2>&1 && git -C "$project_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  detected_branch="$(git -C "$project_root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  detected_commit="$(git -C "$project_root" rev-parse HEAD 2>/dev/null || true)"
  [[ -n "$detected_branch" ]] && git_branch="$detected_branch"
  [[ -n "$detected_commit" ]] && git_commit="$detected_commit"
  if detected_status="$(git -C "$project_root" status --porcelain --untracked-files=normal 2>/dev/null)"; then
    if [[ -z "$detected_status" ]]; then
      git_dirty="false"
    else
      git_dirty="true"
    fi
  fi
fi
fail() { echo "macOS app build: $*" >&2; exit 1; }

[[ "$bundle_identifier" =~ ^[A-Za-z0-9.-]+$ ]] || fail "invalid bundle identifier"
[[ "$display_name" =~ ^[A-Za-z0-9._\ -]+$ ]] || fail "invalid display name"
[[ "$app_bundle_name" =~ ^[A-Za-z0-9._\ -]+$ ]] || fail "invalid app bundle name"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || fail "invalid version"
[[ "$build_number" =~ ^[0-9]+$ ]] || fail "invalid build number"

if [[ "${1:-}" == "--print-build-identity" ]]; then
  printf 'build_kind=%s\n' "$build_kind"
  printf 'bundle_identifier=%s\n' "$bundle_identifier"
  printf 'display_name=%s\n' "$display_name"
  printf 'app_bundle_name=%s\n' "$app_bundle_name"
  printf 'version=%s\n' "$version"
  printf 'build_number=%s\n' "$build_number"
  printf 'git_branch=%s\n' "$git_branch"
  printf 'git_commit=%s\n' "$git_commit"
  printf 'git_dirty=%s\n' "$git_dirty"
  exit 0
fi

[[ "$git_commit" =~ ^[0-9a-f]{40}$ ]] || fail "source commit is unavailable or not a full Git SHA"
[[ "$git_dirty" == "false" ]] || fail "source worktree must be clean and fully committed before building an app"
[[ "$(uname -s)" == "Darwin" ]] || fail "must run on macOS"
[[ -d "$sidecar_root" && ! -L "$sidecar_root" ]] || fail "backend sidecar directory is missing or unsafe; rebuild it from this source commit"
[[ -x "$sidecar_root/SoloScaleBackend" && ! -L "$sidecar_root/SoloScaleBackend" ]] || fail "backend sidecar is missing or unsafe; run packaging/macos/build_backend_onedir.sh first"
sidecar_receipt="$sidecar_root/source-provenance.json"
[[ -f "$sidecar_receipt" && ! -L "$sidecar_receipt" ]] || fail "backend sidecar provenance receipt is missing or unsafe; rebuild the sidecar"
sidecar_commit="$(/usr/bin/plutil -extract source_sha raw -expect string -- "$sidecar_receipt" 2>/dev/null || true)"
sidecar_dirty="$(/usr/bin/plutil -extract dirty raw -expect bool -- "$sidecar_receipt" 2>/dev/null || true)"
[[ "$sidecar_commit" =~ ^[0-9a-f]{40}$ ]] || fail "backend sidecar source commit is invalid"
[[ "$sidecar_commit" == "$git_commit" ]] || fail "backend sidecar source commit does not match the App source commit"
[[ "$sidecar_dirty" == "false" ]] || fail "backend sidecar was not built from a clean source worktree"
[[ -f "$toolchain_config" ]] || fail "toolchain config is missing: $toolchain_config"
SOLOSCALE_TOOLCHAIN_CONFIG="$toolchain_config" "$project_root/scripts/check_macos_toolchain.sh"
# shellcheck disable=SC1090
source "$toolchain_config"
export DEVELOPER_DIR="$SOLOSCALE_DEVELOPER_DIR"
unset SDKROOT
export SDKROOT="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
swift_executable="$(/usr/bin/xcrun --find swift)"
[[ -f "$desktop_root/Package.swift" && -f "$desktop_root/Info.plist.template" ]] || fail "Swift app inputs are missing"
[[ ! -e "$app_root" ]] || fail "output already exists: $app_root"
"$swift_executable" build --package-path "$desktop_root" --scratch-path "$swift_scratch" --configuration release
swift_bin_root="$("$swift_executable" build --package-path "$desktop_root" --scratch-path "$swift_scratch" --configuration release --show-bin-path)"
binary="$swift_bin_root/SoloScaleDesktop"
[[ -x "$binary" ]] || fail "Swift build did not create SoloScaleDesktop"
mkdir -p "$app_root/Contents/MacOS" "$app_root/Contents/Resources"
cp "$binary" "$app_root/Contents/MacOS/SoloScaleDesktop"
/usr/bin/strip -S "$app_root/Contents/MacOS/SoloScaleDesktop"
if LC_ALL=C /usr/bin/grep -a -F -q "$project_root" "$app_root/Contents/MacOS/SoloScaleDesktop"; then
  fail "Swift executable still contains the private build path after stripping"
fi
cp "$desktop_root/Info.plist.template" "$app_root/Contents/Info.plist"
/usr/bin/ditto "$sidecar_root" "$app_root/Contents/Resources/SoloScaleBackend"
/usr/bin/plutil -replace CFBundleIdentifier -string "$bundle_identifier" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleDisplayName -string "$display_name" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleName -string "$display_name" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleShortVersionString -string "$version" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleVersion -string "$build_number" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace SoloScaleBuildKind -string "$build_kind" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace SoloScaleGitBranch -string "$git_branch" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace SoloScaleGitCommit -string "$git_commit" "$app_root/Contents/Info.plist"
/usr/bin/plutil -replace SoloScaleGitDirty -string "$git_dirty" "$app_root/Contents/Info.plist"
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
