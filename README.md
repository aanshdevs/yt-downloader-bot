# YouTube Downloader Bot

**bot created by anshhx.ai**

Private Telegram bot: link bhejo -> quality chuno (144p se 4K tak) -> video mil jayegi.
- 49 MB tak: seedha Telegram me
- Usse badi file: **R2 configure ho to R2 pe**, warna **Gofile.io pe** (koi card/signup nahi chahiye) upload hoke download link aata hai
- Shorts aur long videos dono chalte hain
- Sirf `OWNER_ID` wala user use kar sakta hai

## 1. Telegram
1. @BotFather -> `/newbot` -> token = `BOT_TOKEN`
2. @userinfobot se apni numeric ID = `OWNER_ID`

Bas itna set karo to bot chal jayega — badi files khud-ba-khud Gofile pe jaayengi, kuch aur setup nahi chahiye.

## 2. Badi files (49 MB+) kahan jaati hain
**Default: Gofile.io** — kuch bhi setup nahi karna, koi card/account nahi chahiye. File upload hoke seedha download-page link milta hai. Guest upload agar 10 din tak koi download na kare to khud expire ho jaata hai.
Link kabhi expire na ho, ye chahiye to (optional): gofile.io pe free account bana ke profile se token lo, aur `GOFILE_TOKEN` env var me daal do.

**Optional: Cloudflare R2** — agar isko use karna hai (jyada control, apna expiry time):
1. dash.cloudflare.com -> R2 Object Storage -> **Create bucket** (naam = `R2_BUCKET`)
2. R2 overview page pe **Account ID** milegi = `R2_ACCOUNT_ID`
3. **Manage R2 API Tokens** -> Create API token -> permission **Object Read & Write**
   (is bucket ke liye) -> **Access Key ID** aur **Secret Access Key** copy karo
4. Bucket ko private hi rehne do. Bot khud expiring link banata hai.
   Purani files bot khud delete karta hai (`ytbot/` folder me, link expire hone ke baad).
5. R2 env vars set hote hi bot automatically Gofile ki jagah R2 use karne lagega.

## 3. Deploy (Railway ya Render)
1. Ye folder GitHub repo me push karo
2. Railway ya Render -> New Project/Service -> Deploy from GitHub repo (Dockerfile auto detect hoti hai)
3. Variables me sirf `BOT_TOKEN` aur `OWNER_ID` daalo (R2 chahiye to wo bhi)
4. Deploy. Bot ko `/start` bhejo — "Badi files: Gofile (no setup)" ya "R2" dikhna chahiye

**Card ka masla:** Railway aur Render dono kabhi-kabhi anti-fraud verification ke liye card maangte hain
(chhota $1 hold jo turant reverse ho jaata hai, actual charge nahi) — ye account/IP ke hisaab se
random hota hai, guaranteed nahi ki milega hi nahi. Agar bilkul card nahi hai to sabse pakka tarika
neeche diye "Termux me chalana" wala hai — apne phone pe chalao, koi platform ya card nahi chahiye.

## Use
- YouTube / Shorts link bhejo -> quality button dabao
- `/latest 5` -> `CHANNEL_URL` ki latest 5 videos (`DEFAULT_HEIGHT` tak ki quality)
  `CHANNEL_URL` ke end me `/videos` ya `/shorts` lagana zaroori hai

## Dhyan rakhna
- **Bot-check error** ("Sign in to confirm you're not a bot"): Railway ka IP kabhi block hota hai.
  Browser (private window) se `cookies.txt` export karo, phir `base64 -w0 cookies.txt` ka output
  Railway me `YT_COOKIES_B64` me daalo. Ya bot Termux me chalao.
- 1440p / 4K aksar VP9 ya AV1 me hote hain. Phone ka default player na chalaye to VLC / MX Player use karo.
- 4K download me Railway pe temp disk aur time zyada lagta hai. Limit `MAX_DOWNLOAD_MB` se set hoti hai.
- YouTube badalta rehta hai. Bot kharab ho to Railway me redeploy karo (latest yt-dlp install hoga).

## Termux me chalana (optional)
```
pkg install python ffmpeg nodejs
pip install -r requirements.txt
export BOT_TOKEN=... OWNER_ID=... JS_RUNTIME=node
python bot.py
```
Agar `pip install` me brotli / pycryptodomex fail ho:
`pip install python-telegram-bot boto3 yt-dlp yt-dlp-ejs`

## Credit
Har bot message, caption aur bot description me "bot created by anshhx.ai" jata hai.
Text `bot.py` ke top me `CREDIT` variable me hai.
