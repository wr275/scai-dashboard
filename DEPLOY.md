# SCAI Dashboard — Cloud Deployment Guide

This guide covers deploying to **Railway** (recommended) and **Render** — both have
free tiers and require no server management. Your dashboard gets a public HTTPS URL
in about 5 minutes.

---

## Prerequisites (one-time setup)

1. **GitHub account** — both platforms deploy from Git. Free at github.com
2. **Git installed** — check with `git --version` in your terminal
3. A Railway or Render account (sign up with your GitHub account)

---

## Step 1 — Push your code to GitHub

Open a terminal in the Reporting Dashboard folder and run:

```bash
git init
git add SCAI_Visual_Dashboard.html server.py scai_parser.py \
        requirements.txt Procfile railway.toml render.yaml \
        .gitignore start.bat start.sh
git commit -m "Initial SCAI dashboard"
```

Then create a new **private** repository on github.com (click + → New repository,
name it `scai-dashboard`, set it to Private, do NOT add a README), and run:

```bash
git remote add origin https://github.com/YOUR-USERNAME/scai-dashboard.git
git push -u origin main
```

Your code is now on GitHub. The database and uploaded files are in `.gitignore`
so they never get committed — they live only on the server's persistent disk.

---

## Option A — Railway (recommended, free tier available)

### Deploy

1. Go to **railway.app** and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `scai-dashboard` repository
4. Railway detects the `railway.toml` and starts building automatically

### Add a persistent volume (keeps your data forever)

5. In your Railway project, click **+ New → Volume**
6. Set Mount Path to `/data`
7. Click **Add Volume**

### Set environment variables

8. Click your service → **Variables** tab → **+ New Variable**:

| Variable       | Value              | Notes                          |
|----------------|--------------------|--------------------------------|
| `DATA_DIR`     | `/data`            | Points to the volume you added |
| `DASHBOARD_USR`| `scai`             | Your login username            |
| `DASHBOARD_PWD`| `choose-a-password`| Your login password — pick something strong |

9. Railway redeploys automatically after saving variables

### Get your URL

10. Click **Settings → Domains** → Railway gives you a URL like
    `scai-dashboard-production.up.railway.app`
11. Open that URL — your browser prompts for username/password → enter the values
    you set above → dashboard loads ✅

### Custom domain (optional)

12. In Settings → Domains → click **Custom Domain**
13. Enter e.g. `dashboard.nmdc.sa` or `scai.yourdomain.com`
14. Add the CNAME record your DNS provider (e.g. Cloudflare, GoDaddy) shows

---

## Option B — Render (also free tier)

### Deploy

1. Go to **render.com** and sign in with GitHub
2. Click **New → Web Service**
3. Select your `scai-dashboard` repository
4. Render detects `render.yaml` and pre-fills most settings
5. Scroll to **Environment Variables** and set:
   - `DASHBOARD_USR` = `scai`
   - `DASHBOARD_PWD` = `choose-a-password`
6. Click **Create Web Service**

Render auto-adds a 1 GB disk at `/var/data` from `render.yaml`.
Your URL will be something like `scai-dashboard.onrender.com`.

---

## Deploying updates

Any time you update the dashboard HTML or server code, just push to GitHub:

```bash
git add SCAI_Visual_Dashboard.html server.py scai_parser.py
git commit -m "Update dashboard — added Gantt view"
git push
```

Railway and Render detect the push and redeploy automatically in ~1 minute.
Your database and uploaded files are on the persistent volume — they are
**never touched** by a redeploy.

---

## Security notes

- Always set `DASHBOARD_PWD` before going live. Without it, anyone with the URL
  can see your project data.
- Keep your repository **private** on GitHub.
- For NMDc internal use, consider restricting access by IP in Railway/Render
  settings rather than relying only on the password.
- The `/api/status` endpoint has no auth (used by the platform for health checks)
  but reveals no project data.

---

## File structure reference

```
Reporting Dashboard/
├── SCAI_Visual_Dashboard.html   ← entire frontend
├── server.py                    ← FastAPI backend
├── scai_parser.py               ← Excel parser
├── requirements.txt             ← Python dependencies
├── Procfile                     ← tells the platform how to start
├── railway.toml                 ← Railway-specific config
├── render.yaml                  ← Render-specific config
├── .gitignore                   ← excludes DB and uploads from Git
├── start.bat                    ← local Windows launcher
└── start.sh                     ← local Mac/Linux launcher
```
