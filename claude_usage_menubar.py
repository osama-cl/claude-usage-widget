#!/usr/bin/env python3

import os
import json
import subprocess
import rumps
from datetime import datetime
import shlex
import uuid
from urllib.parse import urlparse


# Config
COLLECTION_FILE = "MyCollection.postman_collection.json"
NEWMAN_OUTPUT_FILE = "output.json"
UPDATE_INTERVAL = 180  # seconds, 3 minutes
STATE_FILE = "notification_state.json"
DEBUG = False  # Set False to disable logs

# Read cURL command from file
CURL_FILE = "curl.txt"
if os.path.exists(CURL_FILE):
    with open(CURL_FILE, "r", encoding="utf-8") as f:
        CURL_COMMAND = f.read().strip()
else:
    debug_log(f"{CURL_FILE} not found!")
    CURL_COMMAND = ""

if not CURL_COMMAND:
    raise RuntimeError("No cURL command found in curl.txt")

# Notification thresholds
THRESHOLDS = [25, 50, 75, 90]

def debug_log(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, **kwargs)


def load_notification_state():
    """Load the state of which notifications have been sent"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "five_hour": {"sent": []},
        "seven_day": {"sent": []}
    }

def save_notification_state(state):
    """Save the notification state to disk"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def should_send_notification(usage_type, current_utilization, state):
    """Check if we should send notifications for any thresholds that were crossed
    Returns only the HIGHEST threshold to avoid notification spam"""
    sent_thresholds = state.get(usage_type, {}).get("sent", [])
    
    # Find all thresholds that should trigger
    thresholds_to_send = []
    for threshold in THRESHOLDS:
        if current_utilization >= threshold and threshold not in sent_thresholds:
            thresholds_to_send.append(threshold)
    
    # To avoid spam, only return the highest threshold
    # But mark all lower ones as sent
    if thresholds_to_send:
        return {
            'notify': [max(thresholds_to_send)],  # Only notify for highest
            'mark_sent': thresholds_to_send       # Mark all as sent
        }
    
    return None

def send_notification(usage_type, threshold, current_utilization):
    """Send a macOS notification using both rumps and osascript"""
    title = f"Claude Usage Alert"
    subtitle = f"{usage_type.replace('_', ' ').title()}"
    message = f"Usage reached {current_utilization}% (threshold: {threshold}%)"
    
    # Method 1: rumps (may not work if app is not signed/notarized)
    try:
        rumps.notification(
            title=title,
            subtitle=subtitle,
            message=message,
            sound=True
        )
    except Exception as e:
        pass
    
    # Method 2: osascript (more reliable)
    try:
        subprocess.run([
            'osascript', '-e',
            f'display notification "{message}" with title "{title}" subtitle "{subtitle}" sound name "default"'
        ], check=True, capture_output=True, text=True)
    except Exception as e:
        pass

def reset_notifications_if_needed(usage_type, current_utilization, state):
    """Reset notification state for any thresholds that usage has fallen below"""
    sent_thresholds = state.get(usage_type, {}).get("sent", [])
    if sent_thresholds:
        # Keep only thresholds that are still at or below current utilization
        state[usage_type]["sent"] = [t for t in sent_thresholds if current_utilization >= t]

def format_reset_time(reset_time_str):
    """Format the reset time in a readable way"""
    try:
        reset_time = datetime.fromisoformat(reset_time_str.replace('Z', '+00:00'))
        now = datetime.now(reset_time.tzinfo)
        
        time_diff = reset_time - now
        
        if time_diff.total_seconds() < 0:
            return "Resetting soon"
        
        hours = int(time_diff.total_seconds() // 3600)
        minutes = int((time_diff.total_seconds() % 3600) // 60)
        
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"Resets in {days}d {hours}h"
        elif hours > 0:
            return f"Resets in {hours}h {minutes}m"
        else:
            return f"Resets in {minutes}m"
    except Exception as e:
        return reset_time_str

def run_newman():
    """Run Newman and export JSON output"""
    debug_log("Running Newman with collection:", COLLECTION_FILE)
    try:
        result = subprocess.run([
            "newman", "run", COLLECTION_FILE,
            "-r", "json",
            "--reporter-json-export", NEWMAN_OUTPUT_FILE
        ], check=True, capture_output=True, text=True)

        debug_log("Newman STDOUT:\n", result.stdout)
        debug_log("Newman STDERR:\n", result.stderr)
        return True
    except subprocess.CalledProcessError as e:
        debug_log("Newman failed with return code:", e.returncode)
        debug_log("Newman STDOUT:\n", e.stdout)
        debug_log("Newman STDERR:\n", e.stderr)
        return False


def get_usage_from_newman_json():
    """Parse the Newman JSON output and return usage info"""
    if not os.path.exists(NEWMAN_OUTPUT_FILE):
        return None, "No data"

    try:
        with open(NEWMAN_OUTPUT_FILE, "r") as f:
            data = json.load(f)

        executions = data.get("run", {}).get("executions", [])
        if not executions:
            return None, "No executions"

        response_stream = executions[0].get("response", {}).get("stream", {}).get("data", [])
        if not response_stream:
            return None, "No response stream"

        # Convert list of ints to string if needed
        if isinstance(response_stream, list):
            response_text = bytes(response_stream).decode("utf-8")
        else:
            response_text = str(response_stream)

        response_json = json.loads(response_text)

        # Extract both five_hour and seven_day usage
        five_hour_data = response_json.get("five_hour", {})
        seven_day_data = response_json.get("seven_day", {})
        
        five_hour = five_hour_data.get("utilization", "N/A")
        seven_day = seven_day_data.get("utilization", "N/A")
        
        five_hour_reset = five_hour_data.get("resets_at", "")
        seven_day_reset = seven_day_data.get("resets_at", "")

        # Concise display: e.g., "5h:56% | 7d:25%"
        display_text = f"5h: {five_hour}% | 7d: {seven_day}%"
        
        return {
            "five_hour": five_hour,
            "seven_day": seven_day,
            "five_hour_reset": five_hour_reset,
            "seven_day_reset": seven_day_reset
        }, display_text

    except Exception as e:
        return None, f"Error: {e}"

def parse_curl(curl_command: str):
    tokens = shlex.split(curl_command)

    method = "GET"
    url = ""
    headers = {}
    body = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token == "curl":
            i += 1
            continue

        if token in ("-X", "--request"):
            method = tokens[i + 1].upper()
            i += 2
            continue

        if token in ("-H", "--header"):
            key, value = tokens[i + 1].split(":", 1)
            headers[key.strip()] = value.strip()
            i += 2
            continue

        if token in ("-d", "--data", "--data-raw", "--data-binary"):
            body = tokens[i + 1]
            if method == "GET":
                method = "POST"
            i += 2
            continue

        if not token.startswith("-"):
            url = token
            i += 1
            continue

        i += 1

    return method, url, headers, body


def generate_postman_collection_from_curl(curl_command: str):
    debug_log("Generating Postman collection from cURL...")

    import shlex
    import uuid
    from urllib.parse import urlparse

    tokens = shlex.split(curl_command)
    method = "GET"
    url = ""
    headers = []
    cookie_header = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token == "curl":
            i += 1
            continue

        # HTTP method
        if token in ("-X", "--request"):
            method = tokens[i + 1].upper()
            debug_log("Detected method:", method)
            i += 2
            continue

        # Header
        if token in ("-H", "--header"):
            key, value = tokens[i + 1].split(":", 1)
            headers.append({"key": key.strip(), "value": value.strip()})
            debug_log(f"Added header: {key.strip()}: {value.strip()}")
            i += 2
            continue

        # Cookie shortcut
        if token in ("-b", "--cookie"):
            cookie_value = tokens[i + 1]
            cookie_header = {"key": "Cookie", "value": cookie_value}
            debug_log("Detected cookie:", cookie_value)
            i += 2
            continue

        # URL
        if not token.startswith("-") and url == "":
            url = token
            debug_log("Detected URL:", url)
            i += 1
            continue

        i += 1

    # If -b/--cookie exists, merge it as a Cookie header (overwrites existing if any)
    if cookie_header:
        # Remove any existing Cookie headers first
        headers = [h for h in headers if h["key"].lower() != "cookie"]
        headers.append(cookie_header)
        debug_log("Final Cookie header set:", cookie_header)

    parsed = urlparse(url)

    collection = {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": "My Collection",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
        },
        "item": [
            {
                "name": url,
                "request": {
                    "method": method,
                    "header": headers,
                    "url": {
                        "raw": url,
                        "protocol": parsed.scheme,
                        "host": parsed.netloc.split("."),
                        "path": parsed.path.strip("/").split("/")
                    },
                    "description": f"Generated from cURL: {curl_command.strip()[:60]}"
                },
                "response": []
            }
        ]
    }

    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(collection, f, indent=2)

    debug_log("Postman collection generated successfully:", COLLECTION_FILE)


class MenuBarApp(rumps.App):
    def __init__(self):
        super(MenuBarApp, self).__init__("Usage")
        self.notification_state = load_notification_state()
        self.next_update_time = None
        self.menu = [
            "Update Now",
            "Check Notification State",
            "Reset Notification History",
            "Test Notification",
            None,  # Separator
            rumps.MenuItem("5-Hour Reset: Loading...", callback=None),
            rumps.MenuItem("7-Day Reset: Loading...", callback=None),
            rumps.MenuItem("Next Update: Loading...", callback=None)
        ]
        
        self.update_usage(None)
        # Run update every interval
        rumps.Timer(self.update_usage, UPDATE_INTERVAL).start()
        # Run countdown timer every second
        rumps.Timer(self.update_countdown, 1).start()

    @rumps.clicked("Update Now")
    def manual_update(self, _):
        self.update_usage(None)

    @rumps.clicked("Test Notification")
    def send_test_notification(self, _=None):
        try:
            rumps.notification(
                title="Claude Usage Monitor",
                subtitle="Test Notification",
                message="If you see this, notifications are working!",
                sound=True
            )
        except Exception as e:
            pass
        
        # Also try using osascript as a fallback
        try:
            subprocess.run([
                'osascript', '-e',
                'display notification "If you see this, notifications are working!" with title "Claude Usage Monitor" subtitle "Test Notification" sound name "default"'
            ])
        except Exception as e:
            pass

    @rumps.clicked("Check Notification State")
    def check_state(self, _):
        state_info = json.dumps(self.notification_state, indent=2)
        rumps.alert(
            title="Notification State",
            message=f"5-Hour sent: {self.notification_state['five_hour']['sent']}\n7-Day sent: {self.notification_state['seven_day']['sent']}"
        )

    @rumps.clicked("Reset Notification History")
    def reset_notifications(self, _):
        self.notification_state = {
            "five_hour": {"sent": []},
            "seven_day": {"sent": []}
        }
        save_notification_state(self.notification_state)
        rumps.alert(title="Reset Complete", message="Notification history has been cleared")

    def update_countdown(self, _):
        """Update the countdown timer display"""
        if self.next_update_time:
            now = datetime.now()
            time_until_update = self.next_update_time - now
            
            if time_until_update.total_seconds() > 0:
                minutes = int(time_until_update.total_seconds() // 60)
                seconds = int(time_until_update.total_seconds() % 60)
                self.menu["Next Update: Loading..."].title = f"Next Update: {minutes}m {seconds}s"
            else:
                self.menu["Next Update: Loading..."].title = "Next Update: Updating..."
        else:
            self.menu["Next Update: Loading..."].title = "Next Update: Loading..."

    def update_usage(self, _):
        if run_newman():
            usage_data, usage_text = get_usage_from_newman_json()
            
            if usage_data:
                # Check and send notifications for five_hour
                if isinstance(usage_data["five_hour"], (int, float)):
                    reset_notifications_if_needed("five_hour", usage_data["five_hour"], self.notification_state)
                    result = should_send_notification("five_hour", usage_data["five_hour"], self.notification_state)
                    
                    if result:
                        # Send notification only for highest threshold
                        for threshold in result['notify']:
                            send_notification("five_hour", threshold, usage_data["five_hour"])
                            import time
                            time.sleep(0.5)  # Small delay to ensure notification is processed
                        
                        # Mark all thresholds as sent
                        for threshold in result['mark_sent']:
                            if threshold not in self.notification_state["five_hour"]["sent"]:
                                self.notification_state["five_hour"]["sent"].append(threshold)
                        
                        save_notification_state(self.notification_state)
                
                # Check and send notifications for seven_day
                if isinstance(usage_data["seven_day"], (int, float)):
                    reset_notifications_if_needed("seven_day", usage_data["seven_day"], self.notification_state)
                    result = should_send_notification("seven_day", usage_data["seven_day"], self.notification_state)
                    
                    if result:
                        # Send notification only for highest threshold
                        for threshold in result['notify']:
                            send_notification("seven_day", threshold, usage_data["seven_day"])
                            import time
                            time.sleep(0.5)  # Small delay to ensure notification is processed
                        
                        # Mark all thresholds as sent
                        for threshold in result['mark_sent']:
                            if threshold not in self.notification_state["seven_day"]["sent"]:
                                self.notification_state["seven_day"]["sent"].append(threshold)
                        
                        save_notification_state(self.notification_state)

                # Always save state to persist any threshold resets
                save_notification_state(self.notification_state)

                # Update menu items with reset times
                five_hour_reset_text = format_reset_time(usage_data["five_hour_reset"])
                seven_day_reset_text = format_reset_time(usage_data["seven_day_reset"])
                
                self.menu["5-Hour Reset: Loading..."].title = f"5-Hour Reset: {five_hour_reset_text}"
                self.menu["7-Day Reset: Loading..."].title = f"7-Day Reset: {seven_day_reset_text}"
            else:
                usage_text = usage_text
        else:
            usage_text = "Newman failed"
        
        self.title = usage_text
        
        # Set next update time
        from datetime import timedelta
        self.next_update_time = datetime.now() + timedelta(seconds=UPDATE_INTERVAL)

if __name__ == "__main__":
    # Generate Postman collection from curl BEFORE running Newman
    generate_postman_collection_from_curl(CURL_COMMAND)
    
    # Start menu bar app
    MenuBarApp().run()

