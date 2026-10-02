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

Render (သို့) သင့် Server တွင် အောက်ပါတွေကို သတ်မှတ်ပါ။

| Variable          | Description                                      | Required |
|-------------------|--------------------------------------------------|----------|
| `TELEGRAM_TOKEN`  | Bot Token from BotFather                        | ✅ |
| `BOT_USERNAME`    | သင့် Bot ၏ Username (မြန်မာလို မဟုတ်, @ မပါ)    | ✅ |
| `ADMIN_ID`        | သင့် Telegram User ID (ဂဏန်း) — ကောင်းမှားထည့်နိုင်     | ✅ |
| `MONGO_URI`       | MongoDB Connection String                       | ✅ |
| `REQUIRED_CHANNELS` | Channel IDs (ကောင်းမှားထည့်နိုင် — မထည့်ရင် default 4 ခုသုံးမယ်) | ❌ |
| `USE_POLLING`     | `true` နဲ့ polling mode — Render **free tier** အတွက် လိုအပ်တယ် | ❌ |
| `WEBHOOK_URL`     | `https://<your-app>.onrender.com/webhook` — webhook mode အတွက် | webhook သုံးရင် |

### 🔀 Webhook vs Polling

| | Webhook | Polling |
|---|---|---|
| Render **Free** | ❌ အလုပ်မလုပ် (spin-down ဖြစ်တတ်) | ✅ |
| Render **Paid** / VPS | ✅ အကြံပြု | ✅ |
| လိုအပ်တဲ့ env | `WEBHOOK_URL` | `USE_POLLING=true` |

`WEBHOOK_URL` မထည့်ရင် (သို့မဟုတ်) `USE_POLLING=true` မရှိရင် polling mode ကို **အလိုအလျောက်** သုံးပါမယ်။

## 🚀 Deploy to Render

1. GitHub တွင် repository push လုပ်ပါ။
2. Render Dashboard → **New Web Service** → Connect Repository.
3. Environment Variables အားလုံး ထည့်ပါ။ (Free tier ဆိုရင် `USE_POLLING=true` ထည့်ပါ)
4. Deploy လုပ်ပါ။ Logs မှာ `🌐 Mode: POLLING` ဆိုတာ ပေါ်လာပါမယ်။

> **မှတ်ချက်:** `Procfile` က `web: python app.py` ဖြစ်ပါတယ်။
> `app.py` က Flask (Waitress) ကို thread ထဲမှာတစ်ပြိုင်နက် serve လုပ်ပြီး Telegram bot loop ကို main thread မှာ run လုပ်ပါတယ်။

## 📝 အသုံးပြုပုံ

### Admin အတွက်

- `/post` – Poster ပုံ (album အများ) → ရုပ်ရှင်ဖိုင် → caption စာသား။
  - ရုပ်ရှင်ဖိုင်ပို့ပြီးနောက် အလိုအလျောက် post မဖြစ်ဘဲ caption စောင့်ပါတယ်။
  - caption မလိုဘူးဆိုရင် `aa` ဟု ရိုက်ပါ။
  - Album ပုံတွေကို အလိုအလျောက် ၁၀ ခုအတွင်းစီ ခွဲပြီး ပို့ပါတယ်။
- `/post_text` – Poster ပုံ → ရုပ်ရှင်ဖိုင် → ဇာတ်ညွှန်းစာသား။
  - ဇာတ်ညွှန်း ၁၀၂၄ စာလုံးကျော်ရှည်ရင် Telegraph page ဖန်တီးပြီး link ပေးပါတယ်။
  - ဇာတ်ညွှန်းမလိုဘူးဆိုရင် `aa` ဟု ရိုက်ပါ။
- `/cancel` – လက်ရှိ conversation ကို ပယ်ဖျက်ပါတယ်။
- Video/Document ဖိုင်တစ်ခုခုပဲ ပို့လို့ရပါတယ် → Deep Link ချက်ချင်းပြန်ပါတယ်။
- Album (တစ်ခါတလက ရုပ်ရှင်ဖိုင် အများ) ပို့လို့ရပါတယ် → အလိုအလျောက် ၁.၂.၃.၄… အစဉ်လိုက် Deep Link စာရင်း။ `/done` ဖြင့်လည်း ချက်ချင်းထုတ်နိုင်ပါတယ်။
- `/stats` – အသုံးပြုသူနှင့် တောင်းဆိုမှုအရေအတွက်။
- `/broadcast` – အသုံးပြုသူအားလုံးသို့ မက်ဆေ့ဂျ်ပို့ရန်။
- `/delete` – Deep Link ဖျက်ရန်။
- `/menu` – Admin မီနူး။

### သုံးစွဲသူများအတွက်

- Deep Link ကို နှိပ်ပါ → လိုအပ်သော Channel များအားလုံးဝင်ပါ → Video ရရှိမည်။

## 🙏 မှတ်ချက်

ဤ Bot သည် **မြန်မာစာ** အဓိကသုံးထားပြီး ဇာတ်ညွှန်းများကို သဘာဝကျကျ ပြန်ဆိုပေးပါသည်။ အဆင်ပြေပါက Star ပေးခဲ့ပါ။
