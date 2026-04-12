# Deployment Guide

This guide covers hosting the Execution Engine Scheduler on free and paid platforms.

## Option 1: Railway (Recommended)

Railway is the easiest option with a free tier ($5 credit/month).

### Setup Steps

1. **Create a Railway account**: https://railway.app

2. **Connect your GitHub repository**:
   - New Project → GitHub repo → hrwatts/clickup-engine
   - Railway will detect `railway.json` and auto-configure

3. **Add environment variables**:
   - Go to project settings → Variables
   - Add all required variables from `.env.example`:
     ```
     CLICKUP_API_TOKEN=...
     PIPELINE_SPACE_NAME=...
     PIPELINE_FOLDER_NAME=...
     SECRET_KEY=...
     DEPLOYED_URL=https://<your-app>.railway.app
     CLICKUP_WEBHOOK_SECRET=...
     ```

4. **Deploy**:
   - Railway auto-deploys on push to `main`
   - Or manually trigger via Railway dashboard

5. **Get your public URL**:
   - Railway dashboard shows your app URL
   - Use this for ClickUp webhooks and mobile shortcuts

### Configuring ClickUp Webhooks

In ClickUp:
1. Go to Workspace → Settings → Apps
2. Click "Webhooks"
3. New Webhook:
   - URL: `https://<your-app>.railway.app/webhook/clickup`
   - Event: `*` (all events)
   - Name: "Execution Engine"

### Cost

- **Free tier**: $5/month credit (usually covers dev/hobby use)
- **Paid**: $5-10/month for production use
- No credit card required for free tier trial

---

## Option 2: Render

Render offers free tier with some limitations.

### Setup Steps

1. **Create Render account**: https://render.com

2. **Connect GitHub**:
   - New → Web Service
   - Connect GitHub repo
   - Select `hrwatts/clickup-engine`

3. **Configure**:
   - **Name**: `clickup-engine` (or your choice)
   - **Environment**: `Python 3.12`
   - **Build**: `pip install -r requirements.txt`
   - **Start**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

4. **Environment variables**:
   - In Render dashboard → Environment
   - Add all vars from `.env.example`

5. **Deploy**:
   - Click Deploy
   - Render auto-deploys on push to `main`

6. **Get URL**: Shown in Render dashboard (e.g., `https://clickup-engine.onrender.com`)

### ClickUp Webhooks

Same as Railway:
- URL: `https://clickup-engine.onrender.com/webhook/clickup`

### Cost

- **Free tier**: Limited (spins down after 15 min inactivity)
- **Paid**: $7/month for always-on
- Web services on free tier are good for testing only

---

## Option 3: Fly.io

Fly.io offers global deployment with free tier credits.

### Setup Steps

1. **Install flyctl**: https://fly.io/docs/hands-on/installing/

2. **Create app**:
   ```bash
   fly auth login
   fly launch --image python:3.12
   ```

3. **Configure `fly.toml`**:
   ```toml
   [build]
   builder = "paketobuildpacks"

   [env]
   PYTHONUNBUFFERED = "true"

   [[services]]
   internal_port = 8000
   protocol = "tcp"
   ```

4. **Set secrets**:
   ```bash
   fly secrets set CLICKUP_API_TOKEN=...
   fly secrets set PIPELINE_SPACE_NAME=...
   fly secrets set PIPELINE_FOLDER_NAME=...
   fly secrets set SECRET_KEY=...
   fly secrets set DEPLOYED_URL=https://<app>.fly.dev
   fly secrets set CLICKUP_WEBHOOK_SECRET=...
   ```

5. **Deploy**:
   ```bash
   fly deploy
   ```

### ClickUp Webhooks

- URL: `https://<your-app>.fly.dev/webhook/clickup`

### Cost

- **Free tier**: $5/month credit
- **Paid**: Usage-based (~$5-15/month typical)

---

## Option 4: Docker + Self-Hosted

For complete control, host on your own server or VM.

### Requirements

- Linux/macOS/Windows with Docker
- Python 3.12+ (or use Docker image)
- Public IP or domain name

### Steps

1. **Build image**:
   ```bash
   docker build -t clickup-engine .
   ```

2. **Run container**:
   ```bash
   docker run -d \
     -p 8000:8000 \
     -e CLICKUP_API_TOKEN=... \
     -e PIPELINE_SPACE_NAME=... \
     -e PIPELINE_FOLDER_NAME=... \
     -e SECRET_KEY=... \
     -e DEPLOYED_URL=https://yourdomain.com \
     --name clickup-engine \
     clickup-engine
   ```

3. **Setup reverse proxy** (nginx/Apache) for HTTPS

4. **Configure ClickUp webhooks** with your public URL

### Cost

- Free if you already have a server
- ~$5-10/month for small VPS

---

## Environment Variables Reference

All deployments need these environment variables:

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `CLICKUP_API_TOKEN` | Your ClickUp API token | `pk_...` |
| `PIPELINE_SPACE_NAME` | ClickUp space containing Execution Engine | `My Workspace` |
| `PIPELINE_FOLDER_NAME` | ClickUp folder containing Execution Engine list | `Projects` |
| `SECRET_KEY` | Random secret for session encryption | Any long random string |

### Required (for webhooks)

| Variable | Description | Example |
|----------|-------------|---------|
| `DEPLOYED_URL` | Your public app URL | `https://clickup-engine.railway.app` |
| `CLICKUP_WEBHOOK_SECRET` | Webhook signing secret (from ClickUp) | Can be any string you choose |

### Optional (for notifications)

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID | `12345678` |

### Optional (advanced)

| Variable | Description | Default |
|----------|-------------|---------|
| `DEBUG` | Enable debug logging | `false` |
| `WORKERS` | Number of Uvicorn workers | `1` |
| `LOG_LEVEL` | Log level (debug, info, warn, error) | `info` |

---

## Setting Up Telegram Notifications (Optional)

To get push notifications on your phone:

1. **Create a Telegram bot**:
   - Chat with [@BotFather](https://t.me/botfather) on Telegram
   - Send `/start` then `/newbot`
   - Choose name and username
   - Copy the token (looks like `123456:ABC...`)

2. **Get your chat ID**:
   - Add your bot to a chat
   - Send a message in that chat
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   - Look for `"from":{"id":YOUR_CHAT_ID}`

3. **Add to environment**:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=12345678
   ```

4. **Test**:
   - Visit `https://your-app.com/webhook/telegram/test`
   - You should get a Telegram message

---

## Monitoring & Logs

### Railway

- Dashboard → Logs tab shows real-time output
- Set up alerts for failures

### Render

- Logs tab shows all output
- Email notifications for build failures

### Fly.io

- `fly logs` command shows real-time logs
- `fly logs --history` shows past logs

### Self-Hosted

```bash
docker logs -f clickup-engine
```

---

## Troubleshooting Deployments

### App crashes on startup

Check logs for errors:
- Missing environment variables
- Invalid ClickUp credentials
- Port already in use

**Fix**: Add all variables from `.env.example`

### Webhooks not being received

- Verify `DEPLOYED_URL` is correct in environment
- Check ClickUp webhook configuration matches your URL
- Look for 401/403 errors in logs (check `CLICKUP_WEBHOOK_SECRET`)

### Slow response times on free tier

- Free tiers spin down after inactivity
- First request after spin-down is slow (cold start)
- Upgrade to paid tier if this bothers you

### "Cannot find space or folder"

- Verify `PIPELINE_SPACE_NAME` and `PIPELINE_FOLDER_NAME` exactly match ClickUp
- They are case-sensitive!

### OutOfMemory errors

- Increase dyno/container size
- Check for memory leaks in logs
- Consider pagination adjustments

---

## Scaling

For multiple users or larger deployments:

1. **Use a real database** instead of file storage
   - See `app/store.py` for replacement points
   - PostgreSQL recommended

2. **Add caching layer**
   - Redis for session cache
   - Memcached for ClickUp data cache

3. **Increase workers**
   - Set `WORKERS=4+` in production
   - Use load balancer (Railway/Render handle this)

4. **Monitor performance**
   - Add APM (Application Performance Monitoring)
   - Set up error tracking (Sentry)

---

## Security Checklist

- ✅ Environment variables stored securely (not in repo)
- ✅ HTTPS enforced (Railway/Render/Fly.io provide free SSL)
- ✅ ClickUp webhook signature verified
- ✅ Session tokens expire after 24 hours
- ✅ No sensitive data in logs

For higher security:
- Consider OAuth2 instead of basic auth
- Implement rate limiting per user
- Add audit logging
- Use VPC/private networks

---

## Next Steps

1. [Get Started](GETTING_STARTED.md) - Local development
2. [Architecture](ARCHITECTURE.md) - System design
3. [Execution Engine Guide](EXECUTION_ENGINE_GUIDE.md) - Operating philosophy
4. [Contributing](CONTRIBUTING.md) - Development setup

