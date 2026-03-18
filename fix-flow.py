#!/usr/bin/env python3
"""Fix Marzban user: add xtls-rprx-vision flow for VLESS Reality."""
import httpx
import json
import sys

BASE = "http://localhost:8000"
USERNAME = "admin"

# Read password from bot .env
password = ""
with open("/opt/strunavpn/.env") as f:
    for line in f:
        if line.startswith("MARZBAN_PASSWORD="):
            password = line.strip().split("=", 1)[1]
            break

if not password:
    print("ERROR: Could not read MARZBAN_PASSWORD from /opt/strunavpn/.env")
    sys.exit(1)

client = httpx.Client(base_url=BASE, timeout=30)

# Get token
resp = client.post("/api/admin/token", data={"username": USERNAME, "password": password})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
print(f"Authenticated OK")

# List users
resp = client.get("/api/users", headers=headers)
users_data = resp.json()
users = users_data.get("users", [])
print(f"Found {len(users)} users")

# Update each user: set flow to xtls-rprx-vision
for user in users:
    uname = user["username"]
    print(f"\nUpdating {uname}...")
    
    # Get full user details
    resp = client.get(f"/api/user/{uname}", headers=headers)
    user_detail = resp.json()
    
    # Update proxies with flow
    proxies = user_detail.get("proxies", {})
    if "vless" in proxies:
        proxies["vless"]["flow"] = "xtls-rprx-vision"
    else:
        proxies["vless"] = {"flow": "xtls-rprx-vision"}
    
    # Send update
    update_data = {
        "proxies": proxies,
        "inbounds": user_detail.get("inbounds", {}),
    }
    
    resp = client.put(f"/api/user/{uname}", json=update_data, headers=headers)
    if resp.status_code == 200:
        new_links = resp.json().get("links", [])
        print(f"  OK! Flow set to xtls-rprx-vision")
        if new_links:
            print(f"  New key: {new_links[0][:80]}...")
    else:
        print(f"  ERROR: {resp.status_code} {resp.text}")

print("\nDone! Go to Telegram bot -> My VPN -> Get Key to get updated key.")
print("Delete old profile from Streisand and import the new one.")
