# VPS Deployment Guide

Step-by-step guide for deploying the FX trading bot on a Ubuntu 22.04 VPS using Docker.

---

## Prerequisites

- Ubuntu 22.04 VPS (minimum 1 vCPU, 1 GB RAM)
- Root or sudo access
- A GitHub remote for the repo (private recommended)
- All API keys ready (see `.env.template`)

---

## Step 1 — Provision the VPS

SSH into the server:

```bash
ssh root@<your-vps-ip>
```

Update the system:

```bash
apt-get update && apt-get upgrade -y
```

Create a non-root user for running the bot:

```bash
adduser botuser
usermod -aG sudo botuser
```

Switch to that user for all remaining steps:

```bash
su - botuser
```

---

## Step 2 — Install Docker

```bash
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
```

Allow `botuser` to run Docker without sudo:

```bash
sudo usermod -aG docker botuser
newgrp docker
```

Verify Docker is working:

```bash
docker run hello-world
```

---

## Step 3 — Install Git and gitleaks

Install Git:

```bash
sudo apt-get install -y git
```

Install gitleaks (secret scanner — must be done before your first `git init`):

```bash
GITLEAKS_VERSION="8.18.4"
curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
  | tar -xz -C /tmp
sudo mv /tmp/gitleaks /usr/local/bin/gitleaks
gitleaks version
```

---

## Step 4 — Clone the Repository

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/<your-username>/fx-trading-bot.git
cd fx-trading-bot
```

---

## Step 5 — Scan for Secrets

Run gitleaks on the full codebase before anything else:

```bash
gitleaks detect --source . --verbose
```

The output must be clean — zero secrets detected — before you proceed. If anything is flagged, resolve it first.

---

## Step 6 — Create the .env File

```bash
cp .env.template .env
nano .env
```

Fill in every variable. Save and exit (`Ctrl+X`, `Y`, `Enter`).

Keep `OANDA_ENVIRONMENT=practice` at this stage. Do not switch to `live` until fully validated.

Run gitleaks again to confirm `.env` is not being tracked:

```bash
gitleaks detect --source . --verbose
```

---

## Step 7 — Pin the Anthropic Dependency

Check the exact version pip resolves on this machine:

```bash
pip3 show anthropic | grep Version
```

Open `requirements.txt` and replace the anthropic line with the exact pinned version. For example:

```
anthropic==0.49.0
```

Commit this change before building the image.

---

## Step 8 — Initialise the Git Repo and Push

If not already a git repo (first deployment):

```bash
git init
git remote add origin https://github.com/<your-username>/fx-trading-bot.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

If the repo already exists on the remote, just push:

```bash
git push
```

---

## Step 9 — Build the Docker Image

```bash
docker build -t fx-trading-bot .
```

This will:
- Pull `python:3.11-slim`
- Install all dependencies from `requirements.txt`
- Copy the application code into the image
- Create `data/cache/` and `logs/` directories
- Set `botuser` (UID 1000) as the runtime user

---

## Step 10 — Smoke Test

Before running the full bot, verify the container starts and env vars load correctly:

```bash
docker run --rm --env-file .env fx-trading-bot python -c "
from config.settings import Settings
s = Settings()
print('OANDA account:', s.OANDA_ACCOUNT_ID)
print('Environment:', s.OANDA_ENVIRONMENT)
print('Telegram configured:', bool(s.TELEGRAM_BOT_TOKEN))
"
```

All three lines should print expected values with no errors. If any value is blank, check your `.env` file.

---

## Step 11 — Run the Bot

```bash
docker run -d \
  --name fx-bot \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  fx-trading-bot
```

Flag reference:

| Flag | Purpose |
|------|---------|
| `-d` | Detached — runs in background |
| `--restart unless-stopped` | Auto-restarts on crash or VPS reboot |
| `--env-file .env` | Injects secrets from file, not baked into image |
| `-v $(pwd)/logs:/app/logs` | Persists trade logs outside the container |
| `-v $(pwd)/data:/app/data` | Persists bot state and cache outside the container |

---

## Step 12 — Verify It's Running

Check the container is up:

```bash
docker ps
```

Tail live logs:

```bash
docker logs -f fx-bot
```

You should see the H1 loop initialising, OANDA connection confirmed, and a Telegram alert sent. If the bot is silent or erroring, check the logs before doing anything else.

---

## Step 13 — Post-Deploy Security Check

Run pip-audit to scan for known CVEs in installed packages:

```bash
docker run --rm fx-trading-bot sh -c "pip install pip-audit -q && pip-audit"
```

Resolve any critical or high severity findings before leaving the bot running unattended.

---

## Common Operations

**View running containers:**
```bash
docker ps
```

**Tail live logs:**
```bash
docker logs -f fx-bot
```

**Stop the bot:**
```bash
docker stop fx-bot
```

**Restart the bot:**
```bash
docker restart fx-bot
```

**Halt trading immediately (file kill switch):**
```bash
touch data/KILL_SWITCH
```

**Resume after file kill switch:**
```bash
rm data/KILL_SWITCH
docker restart fx-bot
```

**Rebuild and redeploy after a code update:**
```bash
git pull
docker build -t fx-trading-bot .
docker stop fx-bot && docker rm fx-bot
docker run -d \
  --name fx-bot \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  fx-trading-bot
```

---

## Troubleshooting

**Container exits immediately:**
Check logs: `docker logs fx-bot`
Usually a missing env var or failed OANDA connection.

**OANDA connection refused:**
Verify `OANDA_API_KEY` and `OANDA_ACCOUNT_ID` in `.env`.
Confirm `OANDA_ENVIRONMENT=practice` matches a practice account token.

**Telegram alerts not arriving:**
Confirm `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct.
Send a test message to the bot first to activate the chat.

**Bot stops trading but container is still running:**
Check if the kill switch file exists: `ls data/KILL_SWITCH`
Check `MAX_DAILY_LOSS_PERCENT` — bot halts if daily loss limit is hit.

**Out of memory:**
Increase VPS RAM or add a swap file:
```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
