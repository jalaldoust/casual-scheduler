# EASIEST Deployment - Railway.app (5 Minutes!)

## Why Railway?
- ✅ **No Linux commands needed**
- ✅ **Click a few buttons and you're done**
- ✅ **Free $5/month credit** (enough for this app)
- ✅ **Automatic SSL/HTTPS**
- ✅ **Auto-deploys when you push code**
- ✅ **Built-in domain or use your own**

---

## Step 1: Prepare Your Code (2 minutes)

### Create a `Procfile` in your project folder:

```bash
cd /Users/kasra/Desktop/code/gpu-scheduler
```

Create a file called `Procfile` (no extension) with this content:
```
web: python app.py
```

### Create a `runtime.txt` file:
```
python-3.11
```

### Update `app.py` to use Railway's PORT:

Open `app.py` and find the bottom where it says:
```python
if __name__ == "__main__":
    load_state()
    server = HTTPServer(("0.0.0.0", 8000), SchedulerHandler)
```

Change it to:
```python
if __name__ == "__main__":
    import os
    load_state()
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), SchedulerHandler)
```

---

## Step 2: Push to GitHub (3 minutes)

```bash
cd /Users/kasra/Desktop/code/gpu-scheduler

# Initialize git (if not already)
git init
git add .
git commit -m "Initial commit"

# Create a repo on GitHub.com (click "New Repository")
# Then run:
git remote add origin https://github.com/YOUR_USERNAME/gpu-scheduler.git
git branch -M main
git push -u origin main
```

---

## Step 3: Deploy on Railway (5 minutes)

1. **Go to https://railway.app**
2. Click **"Start a New Project"**
3. Click **"Deploy from GitHub repo"**
4. Sign in with GitHub
5. Select your `gpu-scheduler` repository
6. Railway will **automatically deploy** - just wait 2-3 minutes
7. Click on your project → Click **"Generate Domain"**
8. Copy the URL (like `gpu-scheduler.up.railway.app`)

**DONE!** Your app is live! 🎉

---

## Step 4: Use Your Own Domain (Optional - 2 minutes)

1. In Railway, click **"Settings"** → **"Domains"**
2. Click **"Custom Domain"**
3. Enter your domain: `scheduler.yourdomain.com`
4. Railway will show you DNS records to add
5. Go to your domain registrar (GoDaddy, Namecheap, etc.)
6. Add the CNAME record Railway shows you
7. Wait 5-10 minutes for DNS to propagate

**DONE!** Now accessible at `https://scheduler.yourdomain.com` 🎉

---

## Cost: FREE for 5 hours/day, or $5/month for 24/7

Railway gives you:
- **$5 free credit/month** (enough for hobby projects)
- After that: ~$5/month for 24/7 uptime
- SSL/HTTPS included free
- No credit card needed to start

---

## Even EASIER Option: Render.com (FREE Forever!)

If you want 100% free (but slower):

1. **Go to https://render.com**
2. Click **"Get Started for Free"**
3. Connect your GitHub account
4. Click **"New Web Service"**
5. Select your `gpu-scheduler` repo
6. Settings:
   - **Name**: gpu-scheduler
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt` (leave empty if no requirements)
   - **Start Command**: `python app.py`
7. Click **"Create Web Service"**
8. Wait 3-5 minutes

**DONE!** Free URL: `https://gpu-scheduler.onrender.com`

### Render Free Tier:
- ✅ **100% FREE forever**
- ✅ SSL included
- ⚠️ Sleeps after 15 minutes of inactivity (takes 30 seconds to wake up)
- Good for low-traffic apps

---

## Which One Should You Use?

| Option | Cost | Speed | Best For |
|--------|------|-------|----------|
| **Railway** | $5/month | Fast | Production (recommended) |
| **Render** | FREE | Slower (sleeps) | Testing/Low traffic |
| **Oracle Cloud** | FREE | Fast | If you know Linux |

**My Recommendation: Railway** - It's worth $5/month for the ease and reliability.

---

## Troubleshooting

**Railway app crashes:**
- Check logs in Railway dashboard
- Make sure `Procfile` and `runtime.txt` are in the root folder

**Render app sleeps:**
- That's normal on free tier
- Upgrade to $7/month for 24/7 uptime

**Can't connect to app:**
- Check the logs in the dashboard
- Make sure `PORT` environment variable is used in `app.py`

---

## Update Your App Later

Just push to GitHub:
```bash
git add .
git commit -m "Updated something"
git push
```

Railway/Render will **automatically redeploy** in 2 minutes! 🚀

---

**Total Setup Time: 10 minutes**
**Difficulty: Copy/paste a few things** ✅
