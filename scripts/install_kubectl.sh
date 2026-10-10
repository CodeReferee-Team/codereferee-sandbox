#!/usr/bin/env bash
# kubectl 바이너리를 .runtime/tools/kubectl 로 받아 체크섬 검증 후 설치한다.
# install_kind.sh 와 같은 구조다. 새 호스트에는 kubectl 이 없는 경우가 많은데
# (Amazon Linux 2023 에 기본 포함되지 않는다) 부트스트랩은 그것을 전제하고 있었다.
set -euo pipefail

# 클러스터와 한 마이너 이상 벌어지지 않도록 kind 가 만드는 노드 버전에 맞춘다.
VERSION="${1:-v1.37.1}"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must look like v1.37.1." >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT/.runtime/tools"
mkdir -p "$TOOL_DIR"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"   # darwin | linux
case "$OS" in
  darwin|linux) ;;
  *) echo "Unsupported OS: $OS. Windows users should install kubectl themselves." >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="amd64" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

BASE_URL="https://dl.k8s.io/release/${VERSION}/bin/${OS}/${ARCH}"
DOWNLOAD="$TOOL_DIR/kubectl.download.$$"
CHECKSUM="$TOOL_DIR/kubectl.sha256.$$"
DEST="$TOOL_DIR/kubectl"
trap 'rm -f "$DOWNLOAD" "$CHECKSUM"' EXIT

curl -fsSL "$BASE_URL/kubectl" -o "$DOWNLOAD"
curl -fsSL "$BASE_URL/kubectl.sha256" -o "$CHECKSUM"

EXPECTED="$(awk '{print tolower($1)}' "$CHECKSUM" | head -1)"
if command -v shasum >/dev/null 2>&1; then
  ACTUAL="$(shasum -a 256 "$DOWNLOAD" | awk '{print tolower($1)}')"
else
  ACTUAL="$(sha256sum "$DOWNLOAD" | awk '{print tolower($1)}')"
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "kubectl checksum mismatch: expected $EXPECTED, got $ACTUAL" >&2
  exit 1
fi

chmod +x "$DOWNLOAD"
mv -f "$DOWNLOAD" "$DEST"
echo "Installed kubectl $VERSION at $DEST"
