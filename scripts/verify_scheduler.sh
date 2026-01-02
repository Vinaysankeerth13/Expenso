#!/bin/bash

# Define variables
PLIST_NAME="com.srisan.expenso.daily.plist"
PLIST_PATH="$(pwd)/scripts/$PLIST_NAME"
DEST_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
PROJECT_DIR="/Users/srisan/Projects/Expenso"

echo "---------------------------------------------------"
echo "Expenso Scheduler Verification"
echo "---------------------------------------------------"

# 1. Check if we are in the right directory
if [[ "$(pwd)" != "$PROJECT_DIR" ]]; then
    echo "⚠️  Please run this script from the project root: $PROJECT_DIR"
    echo "Current directory: $(pwd)"
    # Optional: cd to project dir? No, let's force user to be aware.
fi

# 2. Check PLIST content validity
if grep -q "Desktop/Expenso" "$PLIST_PATH"; then
    echo "❌ Error: Plist file still contains 'Desktop/Expenso'. Please update it."
    exit 1
else
    echo "✅ Plist paths look updated (no 'Desktop' references)."
fi

# 3. Copy/Link Plist
echo "-> Copying plist to $DEST_PLIST..."
cp "$PLIST_PATH" "$DEST_PLIST"

# 4. Unload and Load
echo "-> Reloading launchd job..."
launchctl unload "$DEST_PLIST" 2>/dev/null
launchctl load "$DEST_PLIST"

# 5. Check Status
echo "-> Checking job status..."
launchctl list | grep "com.srisan.expenso.daily"

# 6. Test Run (Kickstart)
echo "---------------------------------------------------"
echo "Would you like to force run the job now to verify? (y/n)"
read -r response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo "-> Kickstarting job..."
    launchctl kickstart -k gui/$(id -u)/com.srisan.expenso.daily
    echo "-> Job started. Checking logs in 5 seconds..."
    sleep 5
    echo "--- STDOUT ---"
    tail -n 10 "$PROJECT_DIR/logs/launchd.log"
    echo "--- STDERR ---"
    tail -n 10 "$PROJECT_DIR/logs/launchd.error.log"
else
    echo "Skipping test run."
fi
echo "---------------------------------------------------"
echo "Done."
