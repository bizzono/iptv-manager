#!/usr/bin/env bash
# install.sh — Install IPTV Manager on macOS (Mac mini / Apple Silicon)
# Run as the 'tower' user: bash install.sh
set -e

INSTALL_DIR="$HOME/playlists"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.tower.iptv-manager.plist"
PYTHON=$(which python3)

echo "=== IPTV Manager Installer ==="
echo "Install dir: $INSTALL_DIR"
echo "Python:      $PYTHON"

# 1. Create install directory
mkdir -p "$INSTALL_DIR"

# 2. Copy files
cp iptv_manager.py "$INSTALL_DIR/"
[ -f channels.csv ] && cp channels.csv "$INSTALL_DIR/" || \
  echo "name,group,resolution,fps,bitrate,tvg_id,tvg_logo,url,source" > "$INSTALL_DIR/channels.csv"

# 3. Install Flask
echo "Installing Flask..."
"$PYTHON" -m pip install --quiet flask

# 4. Write launchd plist
mkdir -p "$PLIST_DIR"
SERVER_HOST=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tower.iptv-manager</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$INSTALL_DIR/iptv_manager.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SERVER_HOST</key>
        <string>$SERVER_HOST</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/iptv-manager.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/iptv-manager.log</string>
</dict>
</plist>
PLIST

# 5. Load service
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

sleep 2

# 6. Verify
if curl -sf "http://localhost:8765/api/status" > /dev/null; then
    echo ""
    echo "✓ IPTV Manager running at http://$SERVER_HOST:8765"
    echo "  M3U:   http://$SERVER_HOST:8765/playlist.m3u"
    echo "  XMLTV: http://$SERVER_HOST:8765/epg.xml"
    echo ""
    echo "Add to Jellyfin → Dashboard → Live TV:"
    echo "  Tuner:  http://$SERVER_HOST:8765/playlist.m3u"
    echo "  Guide:  http://$SERVER_HOST:8765/epg.xml"
else
    echo "ERROR: Server did not start. Check $INSTALL_DIR/iptv-manager.log"
    exit 1
fi
