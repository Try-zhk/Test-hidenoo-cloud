```env
# Proxy URL (http/socks5/hy2/tuic/vless/vmess)
PROXY_URL_1=http://127.0.0.1:8080
PROXY_URL_2=http://127.0.0.1:8081

# Account username password
HIDEN_ACCOUNT_1=your_email1@gmail.com your_password123
HIDEN_ACCOUNT_2=your_email1@gmail.com your_password123

# Proxy Lock
PROXY_LOCK_1=true/false
PROXY_LOCK_2=true/false

# Telegram Bot Token
TG_TOKEN=your_telegram_bot_token

# Telegram Chat ID
TG_CHAT=your_chat_id

# SMTP_CONFIG
SMTP_CONFIG

{
  "host": "smtp.gmail.com",
  "port": 587,
  "user": "abc@xxx.com",
  "pass": "xxxxxx"
}

# EMAIL_CHAT
EMAIL_CHAT=recipient@example.com
```

## 🔐 GitHub PAT (Required for Auto Cookie Writing)

This `GH_PAT` is used to allow the workflow to **automatically write and update `HIDEN_COOKIES` variables** in the repository.

To set it up:

1. Go to GitHub → `Settings → Developer settings → Personal access tokens → Tokens (classic)`
2. Click **Generate new token (classic)**
3. Configure:

   * **Name**: e.g. `UPDATE_VAR_TOKEN`
   * **Expiration**: Recommended `No expiration`
   * **Scopes**: Select `repo` (required)
4. Generate and copy the token (it will only be shown once)

Then:

1. Go to your repository → `Settings → Secrets and variables → Actions`
2. Click **New repository secret**
3. Add:

   * **Name**: `GH_PAT`
   * **Value**: Paste the token

👉 This token is required so the workflow can **write cookies automatically into repository variables**.

## Disclaimer

This project is provided for **educational and reference purposes only**.

* Redistribution of this project or its contents is strictly prohibited.
* Commercial use is not allowed.
* Any illegal or unauthorized use is strictly forbidden.

By using this project, you agree that the author shall **not be held responsible** for any damages or consequences arising from its use.
