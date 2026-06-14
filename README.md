# Telegrambot-project
# 🎬 Movie Deep Link Bot

ဤ Bot သည် Admin များအတွက် အဆင်ပြေစေရန် ဒီဇိုင်းထုတ်ထားပါသည်။

## ✨ အင်္ဂါရပ်များ

- **အလိုအလျောက် Deep Link** – Admin က Video ဖိုင်ပို့လိုက်သည်နှင့် ချက်ချင်း Deep Link ထုတ်ပေးသည်။
- **မြန်မာလို ဇာတ်ကားရှာခြင်း** – `/movie` command ဖြင့် မြန်မာအမည် (သို့) အင်္ဂလိပ်အမည် ရိုက်ထည့်နိုင်သည်။
- **သဘာဝကျသော ဇာတ်လမ်းပြန်ဆိုချက်** – အင်္ဂလိပ် Plot ကို ရင်းနှီးစွာ မြန်မာလို ဘာသာပြန်ပေးသည်။
- **Post အလိုအလျောက်ဖန်တီးခြင်း** – Poster, ဇာတ်ကားအချက်အလက်, Video Deep Link တို့ကို ပေါင်းစပ်၍ လှပသော Post ထုတ်ပေးသည်။
- **Channel Force Subscribe** – Deep Link ရယူရန် သတ်မှတ်ထားသော Channel 4 ခုလုံးကို ဝင်ရောက်ရမည်။

## 🛠️ လိုအပ်ချက်များ

- Python 3.10+
- MongoDB
- Telegram Bot Token (BotFather မှ)
- OMDB API Key (အခမဲ့) – `5025f95c` (သုံးနိုင်သည်)

## ⚙️ Environment Variables

Render (သို့) သင့် Server တွင် အောက်ပါတို့ကို သတ်မှတ်ပါ။

| Variable          | Description                                      |
|-------------------|--------------------------------------------------|
| `TELEGRAM_TOKEN`  | Bot Token from BotFather                        |
| `BOT_USERNAME`    | သင့် Bot ၏ Username (မြန်မာလို မဟုတ်, @ မပါ)    |
| `ADMIN_ID`        | သင့် Telegram User ID (ဂဏန်း)                   |
| `MONGO_URI`       | MongoDB Connection String                       |

(Optional) `PORT` – default 5000

## 🚀 Deploy to Render

1. GitHub တွင် Repository အသစ်ဖန်တီးပါ။
2. အထက်ပါ `app.py`, `requirements.txt`, `Procfile` များကို upload လုပ်ပါ။
3. Render Dashboard → New Web Service → Connect Repository.
4. Environment Variables အားလုံးထည့်ပါ။
5. Deploy လုပ်ပါ။

## 📝 အသုံးပြုပုံ

### Admin အတွက်

- `/movie` – ဇာတ်ကားအမည်ထည့် → Poster ပုံပို့ → Video ပို့ → Post ရရှိမည်။
- Video ဖိုင်တစ်ခုခု ပို့လိုက်ရုံဖြင့် Deep Link ပြန်ရမည်။
- `/newfile` – Video ပို့ပြီး Deep Link ထုတ်ယူနိုင်သည်။
- `/batchlink` – Video အစုလိုက်ပို့ → `/done` → Deep Link စာရင်း။
- `/stats` – အသုံးပြုသူနှင့် တောင်းဆိုမှုအရေအတွက်။
- `/blocklist`, `/unblock` – Block စီမံခန့်ခွဲရန်။

### သုံးစွဲသူများအတွက်

- Deep Link ကို နှိပ်ပါ → လိုအပ်သော Channel များအားလုံးဝင်ပါ → Video ရရှိမည်။

## 🙏 မှတ်ချက်

ဤ Bot သည် **မြန်မာစာ** အဓိကသုံးထားပြီး ဇာတ်ညွှန်းများကို သဘာဝကျကျ ပြန်ဆိုပေးပါသည်။ အဆင်ပြေပါက Star ပေးခဲ့ပါ။
