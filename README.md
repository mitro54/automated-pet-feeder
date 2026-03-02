# automated-pet-feeder
**This is a small personal project running on a Raspberry Pi to allow pet to get some treats when the owner is away.**

## Setup

Here is a template for my personal setup with this, modify to fit your timezone and paths.

1. Set timezone
`sudo timedatectl set-timezone Europe/Helsinki`

2. Create systemd service
`sudo nano /etc/systemd/system/petfeeder.service`

3. Add the following content to the service file:
```bash
[Unit]
Description=Pet Feeder Controller
After=network.target
[Service]
ExecStart=/usr/bin/python3 /home/user/pet_feeder/controller.py
WorkingDirectory=/home/user/pet_feeder
User=user
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

4. Reload systemd and enable the service
`sudo systemctl daemon-reload`
`sudo systemctl enable petfeeder.service`
`sudo systemctl start petfeeder.service`

5. Check status
`sudo systemctl status petfeeder.service`

6. Make feed.sh executable
`chmod +x /home/user/pet_feeder/feed.sh`

7. Add cron job
`crontab -e`

8. Add the following content to the cron job:
```bash
0 18 * * * /home/user/pet_feeder/feed.sh >> /home/user/pet_feeder/scheduled_feeds.log 2>&1
```