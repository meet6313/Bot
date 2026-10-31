# Trading bot (paper mode first)

A small bot that watches a CoinDCX price, buys when a fast average crosses above a slow
average, and sells when it crosses back. It has a stop-loss, a daily loss limit, and a
password-protected dashboard you open on your iPhone.

**It starts in paper mode (fake money). Live trading is off until you switch it on yourself.**

## What is in the folder

- `bot.py` – the bot and the dashboard server (Python 3.9+, nothing to install)
- `dashboard.html` – the phone page the server shows
- `config.example.json` – settings; copy to `config.json`
- `test_bot.py` – safety tests. Run `python3 -m unittest` (all 26 pass)

## 1. Get a server

Rent a small Ubuntu server (1 GB memory is enough) from a provider such as Vultr or
DigitalOcean. Log in with SSH and upload this folder, for example to `/opt/trading-bot`.

## 2. Set the password and settings

```
cd /opt/trading-bot
cp config.example.json config.json
sudo sh -c 'echo "DASH_TOKEN=pick-a-long-random-password" > /etc/trading-bot.env'
sudo chmod 600 /etc/trading-bot.env
```

The password must be at least 12 characters. Do not reuse another password.

## 3. Keep it running

Create `/etc/systemd/system/trading-bot.service`:

```
[Unit]
Description=Trading bot
After=network-online.target

[Service]
WorkingDirectory=/opt/trading-bot
EnvironmentFile=/etc/trading-bot.env
ExecStart=/usr/bin/python3 /opt/trading-bot/bot.py --config /opt/trading-bot/config.json
Restart=always
User=nobody

[Install]
WantedBy=multi-user.target
```

Then: `sudo chown -R nobody /opt/trading-bot && sudo systemctl enable --now trading-bot`

Check it with `sudo systemctl status trading-bot` and `journalctl -u trading-bot -n 50`.

## 4. Open the dashboard on your iPhone

The bot only listens on the server itself, so nobody on the internet can reach it by default.
Pick one way to reach it from your phone:

- **Tailscale (free, no domain needed).** Install Tailscale on the server and on your iPhone,
  set `"host": "0.0.0.0"` in `config.json`, and block public access to port 8080 with the
  server firewall (allow it only on the `tailscale0` interface). Open
  `http://<server tailscale name>:8080` in Safari.
- **HTTPS with a domain.** Point a domain at the server and put Caddy in front with
  `reverse_proxy 127.0.0.1:8080`. Keep `"host": "127.0.0.1"`.

Follow each tool's own setup guide for the exact commands.

Then in Safari: Share, Add to Home Screen. Tick "Remember on this phone" if you want to
skip typing the password.

## 5. Paper trade for 2-4 weeks

With `"mode": "paper"` and `"price_source": "coindcx"` the bot uses real CoinDCX prices with
fake money. The first trade can only happen after about 22 candles, so with the default
5-minute candles wait roughly two hours after starting.

Check whether it makes money after fees. If it does not, do not go live.

## 6. Going live (only when you are ready)

1. On CoinDCX create an API key with **trading permission only**. Never allow withdrawals.
   If CoinDCX lets you limit the key to your server's IP address, do that.
2. Read the order documentation at https://docs.coindcx.com and check that the order request
   in `LiveExec` in `bot.py` matches it. **This part was not tested against a real account.**
3. Add the keys to `/etc/trading-bot.env`:
   ```
   COINDCX_KEY=...
   COINDCX_SECRET=...
   LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY
   ```
4. In `config.json` set `"mode": "live"` and keep `"max_order_inr"` small (for example 500).
5. Restart the bot and watch the first trades yourself. The dashboard shows a red LIVE badge.

The dashboard cannot switch the bot to live mode. That can only be done on the server.

## Safety rules built in

- One position at a time, and only a set % of cash per trade
- Stop-loss checked on every price check, not just once per candle
- Daily loss limit: sells everything and stops trading for the day
- Stops itself after repeated price errors or any failed order, and tells you
- The bot only sells what it bought itself
- "Sell all & stop" button
- Wrong passwords are slowed down and settings are range-checked

Optional phone alerts: set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in the env file.

## Honest limits

- Simple crossover rules often lose money in sideways markets, and fees add up on every trade.
  Check CoinDCX's current fee schedule; the paper mode assumes 0.1% per trade.
- In live mode the profit figures are estimates based on the last price, not exact fills.
- The bot checks prices every few seconds. It is not built for fast trading.
- Nothing here is financial advice. Only risk money you can afford to lose.
