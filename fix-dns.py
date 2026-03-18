#!/usr/bin/env python3
"""Fix xray config: add DNS section for proper name resolution."""
import json

CONFIG = "/var/lib/marzban/xray_config.json"

with open(CONFIG) as f:
    config = json.load(f)

# Add DNS
config["dns"] = {
    "servers": [
        "https+local://1.1.1.1/dns-query",
        "https+local://8.8.8.8/dns-query",
        "1.1.1.1",
        "8.8.8.8"
    ]
}

# Make sure sniffing has routeOnly
for inbound in config.get("inbounds", []):
    if "sniffing" in inbound:
        inbound["sniffing"]["routeOnly"] = True

with open(CONFIG, "w") as f:
    json.dump(config, f, indent=2)

print("Config updated with DNS servers.")
print("Restarting Marzban...")

import subprocess
subprocess.run(["docker", "restart", "marzban-marzban-1"], check=True)
print("Done! Wait 10 seconds, then try VPN again.")
