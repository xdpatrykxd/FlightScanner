# AirScout

AirScout checks Google Flights fares for saved trips and destination lists, keeps scan history in SQLite, and sends price alerts to WhatsApp through WAHA. It runs as a small Docker Compose stack: the web app, a persistent Google browser, and the WhatsApp gateway.

## Quick install on an Ubuntu VPS

These steps are for a fresh Ubuntu 22.04 or 24.04 server. The app and browser ports bind to the server's loopback interface; use the SSH tunnel below to open them securely from your computer. Do not open ports 18081 or 6080 to the public internet.

### 1. Install Docker on the server

Connect to the server over SSH, then run:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl rsync
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run hello-world
```

Docker's `docker` group grants root-level access. Either prefix the Docker commands below with `sudo`, or add your account to that group and reconnect to the server:

```bash
sudo usermod -aG docker "$USER"
```

### 2. Copy AirScout to the server

From a terminal on your computer, go to the AirScout project folder and copy the source files. Replace `SERVER_USER` and `SERVER_IP` with your SSH username and server address:

```bash
cd /path/to/Flight-tracker
rsync -av --exclude='.env' --exclude='data/' --exclude='.git/' ./ SERVER_USER@SERVER_IP:~/airscout/
```

This intentionally leaves local credentials, WhatsApp sessions, browser profile, and scan database behind. If you keep this project in a Git repository, you can instead clone that repository on the server.

### 3. Create the server's private settings and start AirScout

Back in the server SSH terminal:

```bash
cd ~/airscout
cp -n .env.example .env
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.12-slim python scripts/setup_env.py
docker compose up -d --build
docker compose ps
```

The setup script creates a private random WAHA API key and selects the correct WAHA image for an ARM64 or x86-64 server. It preserves existing values. The compose stack starts the web app, Google browser, and WhatsApp gateway. The first build can take a few minutes.

### 4. Open the app securely from your computer

Keep this SSH tunnel running in a terminal on your computer; replace the same two server placeholders:

```bash
ssh -N -L 18081:127.0.0.1:18081 -L 6080:127.0.0.1:6080 SERVER_USER@SERVER_IP
```

Open **http://localhost:18081** in your browser. In **Settings**, connect WhatsApp and scan the QR code with the sender phone using **WhatsApp → Settings → Linked devices → Link a device**. The sender phone must already belong to the group you want to alert. Then choose **Load my groups**, select the group, and send a test message. For the advanced Google Flights sign-in browser, open **http://localhost:18081/admin**; it is available at **http://localhost:6080** while the SSH tunnel is running.

Go to **My alerts** to create a specific-trip alert or a multi-destination budget alert. Choose the group for the alert and save. Set an earliest and latest departure, then an earliest and latest return. AirScout checks every valid departure and return combination inside those ranges and never searches outside them. Results show the exact dates and the savings against the range endpoints when available. Wider ranges add Google Flights searches for every valid date pair. AirScout runs an initial check after you save. Use **Check prices now** on Overview for an additional live check. The default automatic interval is three hours; set `CHECK_INTERVAL_MINUTES` in `.env` if you want a different schedule.

### 5. Everyday server commands

Run these from `~/airscout` on the server:

```bash
docker compose ps                         # See whether services are running
docker compose logs -f airscout           # Follow app logs; Ctrl+C exits the log view
docker compose logs --tail=100 whatsapp-gateway
docker compose up -d                      # Start after a server restart
docker compose down                       # Stop all services; saved data remains
docker compose up -d --build              # Rebuild after updating app files
```

The containers use `restart: unless-stopped`, so they start again when Docker/the server restarts. For another application update, copy the changed source files to the server and run `docker compose up -d --build`.

## Settings and notifications

The `.env` file is private and must not be committed or shared. Common settings:

| Variable | Purpose | Default |
| --- | --- | --- |
| `CHECK_INTERVAL_MINUTES` | Time between scheduled scans | `180` (3 hours) |
| `REQUEST_TIMEOUT_SECONDS` | Per search request timeout | `60` |
| `REQUEST_CONCURRENCY` | Concurrent Google route searches | `1` |
| `DATABASE_PATH` | SQLite database location | `data/listings.sqlite3` |
| `WAHA_API_KEY` | Private key used by AirScout to call WAHA | Generated by setup script |
| `WAHA_IMAGE` | WAHA container image for the server CPU | Selected by setup script |
| `WHATSAPP_SESSION` | WAHA session name | `default` |

The Compose file connects the app to WAHA over its private Docker network. Keep `WAHA_URL=http://whatsapp-gateway:3000` for the Compose setup. `WHATSAPP_GATEWAY_URL` is the host-side address used by command-line tools, normally `http://127.0.0.1:3010`.

You can optionally configure phone numbers or groups directly in `.env` using `WHATSAPP_RECIPIENT_1`, `WHATSAPP_RECIPIENT_2`, `WHATSAPP_RECIPIENTS`, or `WHATSAPP_GROUP_IDS`. Use full international phone numbers (for example `+32470123456`) or WAHA group IDs ending in `@g.us`. Usually it is easier to connect WhatsApp in the app, load groups, and choose a group in the alert setup.

### Price alerts

- A specific-trip alert sends when the best fare reaches its target price.
- A destination alert sends when the cheapest fare is within the budget.
- A major-drop alert sends when the fare is at least 10% below the average of up to the last 10 recorded fares for the same route and travel dates. It needs at least 3 earlier scan samples to establish that average.
- Repeated identical or higher fares are suppressed to avoid duplicate WhatsApp messages.

AirScout reads fare cards visible in Google Flights. Google may omit some airlines, sellers, routes, or prices, and may change its page at any time. AirScout does not reserve or book tickets; open the Google Flights link on a result and recheck the fare before booking.

If AirScout finds no fare for the selected departure day, it retries the following days up to (but not including) the return date. The return date stays fixed. Results show which dates produced the fare.

## Troubleshooting

**The app does not open:** check `docker compose ps` and `docker compose logs --tail=100 airscout`. Confirm the SSH tunnel is still open and visit `http://localhost:18081` on your computer.

**The WhatsApp QR does not appear or the group is missing:** check `docker compose logs --tail=100 whatsapp-gateway`. In the app, reconnect WhatsApp and load groups again. The sender phone must be linked and already a member of the group.

**WhatsApp does not send:** send a test message in Settings. Check the WAHA logs and group permissions. WAHA acceptance confirms that the gateway accepted the request; it does not confirm the group received or read the message.

**Google Flights sign-in window does not open:** keep the SSH tunnel running with both port mappings. Open AirScout’s `/admin` page (or `http://localhost:6080` directly) on your computer. The profile is stored in `data/google-flights-profile/` on the server and should be treated as private.

**Google Flights search fails:** inspect `docker compose logs --tail=100 airscout` and retry later. Google may block automated searches or change its result page. Confirm any fare directly on Google Flights.

## Backups and updates

Back up these items privately: `.env`, `data/listings.sqlite3`, `data/whatsapp-sessions/`, and `data/google-flights-profile/`. They include credentials, linked-device state, saved alerts, or browser sign-in data. Keep copies encrypted and limit access to them.

To update from a local copy, transfer the changed files without overwriting server settings or data, then run `docker compose up -d --build` on the server. To stop the services without deleting data, use `docker compose down`.

## Local testing on macOS or Linux

Install Docker Desktop, open a terminal in the project folder, and run:

```bash
cp -n .env.example .env
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work python:3.12-slim python scripts/setup_env.py
docker compose up -d --build
```

Open **http://localhost:18081**. For Windows PowerShell, use:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker run --rm --mount "type=bind,source=$($PWD.Path),target=/work" -w /work python:3.12-slim python scripts/setup_env.py
docker compose up -d --build
```

## References

- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Google Flights](https://www.google.com/travel/flights)
- [WAHA sessions and QR pairing](https://waha.devlike.pro/docs/how-to/sessions/)
- [WAHA WhatsApp groups](https://waha.devlike.pro/docs/how-to/groups/)
