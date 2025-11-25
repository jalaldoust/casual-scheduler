#!/bin/bash
# Simple deployment script for GPU Scheduler
# Usage: ./deploy-to-server.sh YOUR_SERVER_IP

if [ -z "$1" ]; then
    echo "Usage: ./deploy-to-server.sh YOUR_SERVER_IP"
    exit 1
fi

SERVER_IP=$1

echo "📦 Packaging files..."
cd /Users/kasra/Desktop/code/gpu-scheduler
tar -czf /tmp/gpu-scheduler.tar.gz app.py Procfile runtime.txt static/ data/ 2>/dev/null || tar -czf /tmp/gpu-scheduler.tar.gz app.py Procfile runtime.txt static/

echo "📤 Uploading to server..."
scp /tmp/gpu-scheduler.tar.gz root@$SERVER_IP:/root/

echo "🚀 Setting up server..."
ssh root@$SERVER_IP << 'ENDSSH'
# Install dependencies
apt-get update
apt-get install -y python3 python3-pip nginx

# Extract files
cd /root
tar -xzf gpu-scheduler.tar.gz
mkdir -p data

# Create systemd service
cat > /etc/systemd/system/gpu-scheduler.service << 'EOF'
[Unit]
Description=GPU Scheduler
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/app.py
Restart=always
Environment=PORT=8000

[Install]
WantedBy=multi-user.target
EOF

# Start service
systemctl daemon-reload
systemctl enable gpu-scheduler
systemctl start gpu-scheduler

# Configure nginx
cat > /etc/nginx/sites-available/default << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

systemctl restart nginx

echo "✅ Deployment complete!"
echo "🌐 Your app is running at http://$HOSTNAME"
ENDSSH

echo ""
echo "✅ Done! Your app is running at http://$SERVER_IP"
echo ""
echo "📝 Next steps:"
echo "1. Go to Namecheap DNS settings"
echo "2. Add A Record:"
echo "   Host: @"
echo "   Value: $SERVER_IP"
echo "3. Add A Record:"
echo "   Host: www"
echo "   Value: $SERVER_IP"
echo ""
echo "Wait 5 minutes, then visit https://yourdomain.com"
