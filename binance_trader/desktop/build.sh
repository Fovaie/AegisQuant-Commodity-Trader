#!/bin/zsh
set -eu
cd "$(dirname "$0")/../.."
BUILD_PYTHON="${BUILD_PYTHON:-python3}"
EDITION="${1:-en}"
case "$EDITION" in
  en) APP_NAME="AegisQuant Commodity Trader EN" ;;
  zh-CN) APP_NAME="AegisQuant 商品交易终端 中文版" ;;
  *) print -u2 'Usage: build.sh [en|zh-CN]'; exit 2 ;;
esac
APP="${COMMODITY_APP_OUTPUT:-$PWD/$APP_NAME.app}"
BUILD_DIR="$(mktemp -d /tmp/commodity-build.XXXXXX)"
cp -R binance_trader/static "$BUILD_DIR/static"
printf '{"language":"%s"}\n' "$EDITION" > "$BUILD_DIR/static/edition.json"
xcrun swiftc -swift-version 5 -module-cache-path /private/tmp/commodity-swift-cache binance_trader/desktop/SecureStore.swift -o "$BUILD_DIR/secure-store"
codesign --force --sign - "$BUILD_DIR/secure-store"
"$BUILD_PYTHON" -m PyInstaller --noconfirm --onedir --name commodity-backend --paths "$PWD" --add-binary "$BUILD_DIR/secure-store:." --add-data "$BUILD_DIR/static:binance_trader/static" --distpath "$BUILD_DIR/dist" --workpath "$BUILD_DIR/work" --specpath "$BUILD_DIR" binance_trader/desktop/backend_entry.py
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
if [[ -d "$APP/Contents/Resources/backend" ]]; then
  mv "$APP/Contents/Resources/backend" "$BUILD_DIR/previous-backend"
fi
cp -R "$BUILD_DIR/dist/commodity-backend" "$APP/Contents/Resources/backend"
if [[ -n "${COMMODITY_DATA_PATH:-}" ]]; then
  DATA_PATH="$COMMODITY_DATA_PATH"
elif [[ -f "$APP/Contents/Resources/data-path.txt" ]]; then
  DATA_PATH="$(cat "$APP/Contents/Resources/data-path.txt")"
else
  DATA_PATH="@APPLICATION_SUPPORT@"
fi
printf '%s\n' "$DATA_PATH" > "$APP/Contents/Resources/data-path.txt"
printf '%s\n' "$EDITION" > "$APP/Contents/Resources/edition.txt"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>CommodityTrader</string>
<key>CFBundleIdentifier</key><string>local.frank.commoditytrader</string>
<key>CFBundleName</key><string>$APP_NAME</string>
<key>CFBundleDisplayName</key><string>$APP_NAME</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.1.1</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
xcrun swiftc -swift-version 5 -module-cache-path /private/tmp/commodity-swift-cache binance_trader/desktop/Launcher.swift -o "$APP/Contents/MacOS/CommodityTrader"
codesign --force --deep --sign - "$APP"
printf 'Built: %s\n' "$APP"
