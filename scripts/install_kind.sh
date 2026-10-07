#!/usr/bin/env bash
# install_kind.ps1의 POSIX(mac/linux) 포팅. kind 바이너리를 .runtime/tools/kind로 받아 검증 설치.
set -euo pipefail

VERSION="${1:-v0.33.0}"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must look like v0.33.0." >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT/.runtime/tools"
mkdir -p "$TOOL_DIR"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"   # darwin | linux
case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="amd64" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

ASSET="kind-${OS}-${ARCH}"
BASE_URL="https://github.com/kubernetes-sigs/kind/releases/download/${VERSION}"
DOWNLOAD="$TOOL_DIR/$ASSET"
CHECKSUMS="$TOOL_DIR/kind-sha256sum.txt"
DEST="$TOOL_DIR/kind"

curl -fsSL "$BASE_URL/$ASSET" -o "$DOWNLOAD"
curl -fsSL "$BASE_URL/$ASSET.sha256sum" -o "$CHECKSUMS"

EXPECTED="$(awk '{print tolower($1)}' "$CHECKSUMS" | head -1)"
if command -v shasum >/dev/null 2>&1; then
  ACTUAL="$(shasum -a 256 "$DOWNLOAD" | awk '{print tolower($1)}')"
else
  ACTUAL="$(sha256sum "$DOWNLOAD" | awk '{print tolower($1)}')"
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
  rm -f "$DOWNLOAD"
  echo "kind checksum mismatch: expected $EXPECTED, got $ACTUAL" >&2
  exit 1
fi

chmod +x "$DOWNLOAD"
mv -f "$DOWNLOAD" "$DEST"
rm -f "$CHECKSUMS"
echo "Installed kind $VERSION at $DEST"
