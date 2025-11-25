# GPU Scheduler Deployment Guide

## Cheapest & Easiest Hosting Option: Oracle Cloud Free Tier

Oracle Cloud offers a **FREE forever** tier with an ARM-based VM that's perfect for this app. It's 100% free (no credit card charges) and very easy to set up.

### What You Get (FREE Forever):
- 1 ARM-based VM (4 CPUs, 24GB RAM)
- 200GB storage
- 10TB bandwidth/month
- Public IP address

---

## Step-by-Step Deployment

### 1. Create Oracle Cloud Account
1. Go to https://www.oracle.com/cloud/free/
2. Sign up for a free account (requires email verification)
3. Complete the registration (you'll need to enter a credit card, but it won't be charged for free tier resources)

### 2. Create a VM Instance
1. Log in to Oracle Cloud Console
2. Click **"Create a VM Instance"**
3. Choose these settings:
   - **Name**: `gpu-scheduler`
   - **Image**: Ubuntu 22.04 (Minimal)
   - **Shape**: Ampere (ARM) - `VM.Standard.A1.Flex` (4 OCPUs, 24GB RAM - all free!)
   - **Network**: Use default VCN (Virtual Cloud Network)
   - **Add SSH Keys**: Generate a new key pair or upload your public key
   - **Boot Volume**: 50GB (plenty for this app)
4. Click **"Create"**
5. Wait 2-3 minutes for the instance to start
6. Copy the **Public IP Address** from the instance details

### 3. Configure Firewall
1. In the instance details, click on the **Subnet** link
2. Click on the **Default Security List**
3. Click **"Add Ingress Rules"**
4. Add these rules:
   - **Source CIDR**: `0.0.0.0/0`
   - **Destination Port**: `80`
   - **Description**: HTTP
5. Click **"Add Ingress Rules"** again for HTTPS:
   - **Source CIDR**: `0.0.0.0/0`
   - **Destination Port**: `443`
   - **Description**: HTTPS

### 4. SSH into Your Server
```bash
ssh ubuntu@YOUR_PUBLIC_IP
```

### 5. Install Dependencies
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+
sudo apt install -y python3 python3-pip

# Install nginx (web server)
sudo apt install -y nginx

# Install certbot (for SSL/HTTPS)
sudo apt install -y certbot python3-certbot-nginx
```

### 6. Upload Your Code
On your local machine:
```bash
# From your project directory
scp -r /Users/kasra/Desktop/code/gpu-scheduler ubuntu@YOUR_PUBLIC_IP:~/
```

Or use git:
```bash
# On the server
cd ~
git clone YOUR_REPO_URL gpu-scheduler
cd gpu-scheduler
```

### 7. Set Up the Application
```bash
cd ~/gpu-scheduler

# Create a systemd service to run the app
sudo tee /etc/systemd/system/gpu-scheduler.service > /dev/null <<EOF
[Unit]
Description=GPU Scheduler
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/gpu-scheduler
ExecStart=/usr/bin/python3 /home/ubuntu/gpu-scheduler/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable gpu-scheduler
sudo systemctl start gpu-scheduler

# Check status
sudo systemctl status gpu-scheduler
```

### 8. Configure Nginx as Reverse Proxy
```bash
sudo tee /etc/nginx/sites-available/gpu-scheduler > /dev/null <<EOF
server {
    listen 80;
    server_name YOUR_DOMAIN.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Enable the site
sudo ln -s /etc/nginx/sites-available/gpu-scheduler /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

### 9. Point Your Domain to the Server
1. Go to your domain registrar (GoDaddy, Namecheap, etc.)
2. Add an **A Record**:
   - **Name**: `@` (or your subdomain like `scheduler`)
   - **Value**: Your Oracle Cloud Public IP
   - **TTL**: 300 (5 minutes)
3. Wait 5-10 minutes for DNS to propagate

### 10. Enable HTTPS (SSL)
```bash
# Get free SSL certificate from Let's Encrypt
sudo certbot --nginx -d YOUR_DOMAIN.com

# Follow the prompts:
# - Enter your email
# - Agree to terms
# - Choose to redirect HTTP to HTTPS (option 2)

# Certbot will automatically renew the certificate
sudo systemctl status certbot.timer
```

### 11. Configure Ubuntu Firewall
```bash
# Allow SSH, HTTP, and HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

## Update the App Port (if needed)

If your `app.py` doesn't run on port 8000, update it:

```python
# At the bottom of app.py, change:
if __name__ == "__main__":
    load_state()
    server = HTTPServer(("0.0.0.0", 8000), SchedulerHandler)
    print("Server running on http://0.0.0.0:8000")
    server.serve_forever()
```

Then restart:
```bash
sudo systemctl restart gpu-scheduler
```

---

## Useful Commands

```bash
# View app logs
sudo journalctl -u gpu-scheduler -f

# Restart the app
sudo systemctl restart gpu-scheduler

# Stop the app
sudo systemctl stop gpu-scheduler

# Check nginx status
sudo systemctl status nginx

# Restart nginx
sudo systemctl restart nginx

# Renew SSL certificate manually
sudo certbot renew
```

---

## Cost: $0/month Forever! 🎉

Oracle Cloud's free tier is permanent and doesn't expire. As long as you use the ARM-based instance (VM.Standard.A1.Flex), it's completely free.

---

## Alternative: DigitalOcean (Paid but Simple)

If you prefer a paid option with better support:

1. **DigitalOcean Droplet**: $6/month (1GB RAM, 1 CPU)
2. Follow the same steps above (Ubuntu 22.04)
3. Use their 1-click apps for easier setup

---

## Troubleshooting

**App won't start:**
```bash
sudo journalctl -u gpu-scheduler -n 50
```

**Can't access the site:**
- Check firewall: `sudo ufw status`
- Check nginx: `sudo nginx -t`
- Check DNS: `nslookup YOUR_DOMAIN.com`

**SSL certificate issues:**
```bash
sudo certbot certificates
sudo certbot renew --dry-run
```

---

## Security Notes

1. **Change default passwords immediately** - All users start with password = username
2. **Keep the system updated**: `sudo apt update && sudo apt upgrade -y`
3. **Monitor logs regularly**: `sudo journalctl -u gpu-scheduler -f`
4. **Backup your data**: The state is stored in `data/state.json`

---

That's it! Your GPU scheduler is now live and accessible at https://YOUR_DOMAIN.com 🚀
