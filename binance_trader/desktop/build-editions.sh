#!/bin/zsh
set -eu
cd "$(dirname "$0")/../.."
mkdir -p dist
EN="$PWD/dist/AegisQuant Commodity Trader EN.app"
ZH="$PWD/dist/AegisQuant 商品交易终端 中文版.app"
if [[ -e "$EN" || -e "$ZH" ]]; then
  print -u2 'dist already contains an edition. Move it aside before rebuilding.'
  exit 2
fi
COMMODITY_APP_OUTPUT="$EN" zsh binance_trader/desktop/build.sh en
# Both editions share the same engine and native binary; language is fixed in
# their signed resources, never selected by runtime controls.
ditto "$EN" "$ZH"
printf '{"language":"zh-CN"}\n' > "$ZH/Contents/Resources/backend/_internal/binance_trader/static/edition.json"
printf 'zh-CN\n' > "$ZH/Contents/Resources/edition.txt"
/usr/libexec/PlistBuddy -c 'Set :CFBundleName AegisQuant 商品交易终端 中文版' "$ZH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName AegisQuant 商品交易终端 中文版' "$ZH/Contents/Info.plist"
codesign --force --deep --sign - "$ZH"
for app in "$EN" "$ZH"; do codesign --verify --deep --strict "$app"; done
ditto -c -k --sequesterRsrc --keepParent "$EN" dist/AegisQuant-Commodity-Trader-macOS-EN.zip
ditto -c -k --sequesterRsrc --keepParent "$ZH" dist/AegisQuant-Commodity-Trader-macOS-ZH-CN.zip
(cd dist && shasum -a 256 *.zip > SHA256SUMS.txt)
