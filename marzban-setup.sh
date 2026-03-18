#!/bin/bash
# StrunaVPN — Marzban Setup
# Ставит VPN-панель на тот же сервер где бот
set -e

echo "=== StrunaVPN — Marzban Setup ==="

MARZBAN_PASS=$(openssl rand -hex 12)
SHORT_ID=$(openssl rand -hex 4)
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

# 1. Install docker-compose plugin if missing
echo "[1/4] Checking Docker..."
apt install -y docker-compose-v2 2>/dev/null || apt install -y docker-compose 2>/dev/null || true

# 2. Install Marzban
echo "[2/4] Installing Marzban..."
if [ ! -d "/opt/marzban" ]; then
    mkdir -p /opt/marzban
    # Download docker-compose for Marzban
    curl -sL https://raw.githubusercontent.com/Gozargah/Marzban/master/docker-compose.yml -o /opt/marzban/docker-compose.yml
    
    # Fix: ensure we use correct image
    cat > /opt/marzban/docker-compose.yml << 'DCEOF'
services:
  marzban:
    image: gozargah/marzban:latest
    restart: always
    network_mode: host
    environment:
      UVICORN_HOST: "0.0.0.0"
      UVICORN_PORT: "8000"
      DASHBOARD_PATH: "/dashboard/"
      XRAY_JSON: "/var/lib/marzban/xray_config.json"
    volumes:
      - /var/lib/marzban:/var/lib/marzban
      - /opt/marzban/.env:/code/.env
DCEOF
fi

# 3. Generate Reality keys
echo "[3/4] Generating keys..."
KEYS=$(docker run --rm gozargah/marzban:latest xray x25519 2>/dev/null || echo "")
if [ -n "$KEYS" ]; then
    PRIVATE_KEY=$(echo "$KEYS" | grep "Private" | awk '{print $3}')
    PUBLIC_KEY=$(echo "$KEYS" | grep "Public" | awk '{print $3}')
else
    # Fallback: generate with openssl
    PRIVATE_KEY=$(openssl rand -hex 32)
    PUBLIC_KEY="generate_manually"
fi

# 4. Configure
echo "[4/4] Configuring..."

cat > /opt/marzban/.env << MENV
UVICORN_HOST = "0.0.0.0"
UVICORN_PORT = 8000
DASHBOARD_PATH = "/dashboard/"
SUDO_USERNAME = "admin"
SUDO_PASSWORD = "${MARZBAN_PASS}"
XRAY_JSON = "/var/lib/marzban/xray_config.json"
XRAY_SUBSCRIPTION_URL_PREFIX = "http://${SERVER_IP}:8000"
MENV

mkdir -p /var/lib/marzban

cat > /var/lib/marzban/xray_config.json << XRAY
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "VLESS_REALITY",
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "www.google.com:443",
          "xver": 0,
          "serverNames": ["www.google.com", "google.com"],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}", ""]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "DIRECT" },
    { "protocol": "blackhole", "tag": "BLOCK" }
  ],
  "routing": {
    "rules": [
      { "ip": ["geoip:private"], "outboundTag": "BLOCK", "type": "field" },
      { "type": "field", "protocol": ["bittorrent"], "outboundTag": "BLOCK" }
    ]
  }
}
XRAY

# Start Marzban
cd /opt/marzban
docker compose up -d 2>/dev/null || docker-compose up -d 2>/dev/null
sleep 10

# Update bot .env with real Marzban password
sed -i "s/MARZBAN_PASSWORD=temp123/MARZBAN_PASSWORD=${MARZBAN_PASS}/" /opt/strunavpn/.env
systemctl restart strunavpn

# Check
if curl -s http://localhost:8000/api/admin/token > /dev/null 2>&1; then
    PANEL_STATUS="WORKING"
else
    PANEL_STATUS="STARTING (wait 30 sec and try)"
fi

echo ""
echo "=== MARZBAN READY! ==="
echo ""
echo "Panel:     http://${SERVER_IP}:8000/dashboard/"
echo "Login:     admin"
echo "Password:  ${MARZBAN_PASS}"
echo "Status:    ${PANEL_STATUS}"
echo ""
echo "VPN port:  443 (VLESS Reality)"
if [ "$PUBLIC_KEY" != "generate_manually" ]; then
echo "Pub key:   ${PUBLIC_KEY}"
fi
echo "Short ID:  ${SHORT_ID}"
echo ""
echo "Bot restarted with new Marzban password."
echo "Try /start in Telegram -> My VPN -> Get Key"
echo ""

# Save
cat >> /root/strunavpn-credentials.txt << CREDS

=== Marzban (added $(date)) ===
Panel: http://${SERVER_IP}:8000/dashboard/
Login: admin
Password: ${MARZBAN_PASS}
Public Key: ${PUBLIC_KEY}
Short ID: ${SHORT_ID}
CREDS
echo "Credentials saved to /root/strunavpn-credentials.txt"
