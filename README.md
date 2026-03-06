# SDR Bot

Autonomous prospecting agent. Finds high-propensity hotel lodging accounts, scores them, writes personalized outreach, and executes a 5-touch multi-channel sequence (email + LinkedIn). You approve the accounts and pick the messaging. The bot does everything else.

**Goal:** Beat 6 meetings/month without touching Salesforce or ZoomInfo.

---

## How It Works

```
Every Monday 7 AM
  → Apollo: search companies matching travel-heavy industries
  → Claude: score each company (propensity 0–100)
  → Claude: generate 3 email variants + 2 LinkedIn scripts per account
  → You: receive a digest email with a link to review 50 accounts
  → You: approve/reject each account, pick your preferred message variant
  → On approval: Apollo reveals contact email (1 credit per contact)
  → Bot: sends email Touch 1 immediately
  → Bot: executes 5-touch sequence over 21 days
      Touch 1  — Day 1:  Email (auto-sent)
      Touch 2  — Day 3:  LinkedIn (script in dashboard, you copy/paste)
      Touch 3  — Day 7:  Follow-up email (auto-sent)
      Touch 4  — Day 14: LinkedIn follow-up (script in dashboard)
      Touch 5  — Day 21: Breakup email (auto-sent)

Every 2 hours
  → Bot monitors Gmail for replies
  → Claude classifies reply: hot / neutral / unsubscribe / OOO
  → Hot reply: instant alert email to you + flagged in Hot Leads

```

---

## Setup

### 1. Clone and install

```bash
git clone git@github.com:jaspalsingh-engine/Agent.git
cd Agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your keys
```

Required values:
| Variable | Where to get it |
|---|---|
| `APOLLO_API_KEY` | apollo.io → Settings → API |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `GMAIL_SENDER_ADDRESS` | Your work Gmail address |
| `YOUR_CALENDLY_LINK` | Your Calendly scheduling link |
| `DIGEST_EMAIL_RECIPIENT` | Where you want weekly digests sent (can be same as sender) |

### 3. Set up Gmail OAuth

The bot sends and reads email from your Gmail account using OAuth2 (no password stored).

**Step 1:** Go to [Google Cloud Console](https://console.cloud.google.com)

**Step 2:** Create a new project (or use existing)

**Step 3:** Enable the **Gmail API**
- APIs & Services → Enable APIs → search "Gmail API" → Enable

**Step 4:** Create OAuth 2.0 credentials
- APIs & Services → Credentials → Create Credentials → OAuth client ID
- Application type: **Desktop app**
- Download the JSON file

**Step 5:** Save credentials
```bash
mkdir credentials
# Move the downloaded file to:
mv ~/Downloads/client_secret_*.json credentials/credentials.json
```

**Step 6:** Authorize (one-time)
```bash
python3 -c "from app.gmail import _get_service; _get_service()"
# A browser window will open — log in and grant access
# token.json is saved to credentials/ automatically
```

### 4. Run the app

```bash
uvicorn app.main:app --host localhost --port 8000
```

Open `http://localhost:8000` in your browser.

---

## Usage

### Weekly flow

1. Monday morning: check your email for the digest
2. Click the review link → approve/reject accounts, select message variants
3. Approved accounts: email sends automatically, LinkedIn scripts appear in the LinkedIn Queue
4. Check **LinkedIn Queue** daily for scripts to copy and send manually
5. Check **Hot Leads** for replies that need follow-up

### Manual triggers (for testing)

From the dashboard, click **"Run Discovery Now"** to run a batch immediately without waiting for Monday.

Or via the terminal while the app is running:
```bash
curl -X POST http://localhost:8000/admin/run-discovery
curl -X POST http://localhost:8000/admin/run-touches
```

---

## Apollo Credit Strategy

Contacts are **not** revealed until you approve an account. This means:
- 50 accounts in the weekly batch = **0 credits used** upfront
- Each account you approve = **1 credit** (for the primary contact's email)
- If you approve 15 accounts/week = 15 credits/week = ~60 credits/month

Apollo's free plan includes 50 credits/month. Approve selectively or upgrade to Apollo's Basic plan ($49/month) for 200 credits/month.

---

## Targeting Configuration

Edit `TARGET_INDUSTRIES` in your `.env` to focus on different industries. Apollo industry names to use:

- `Construction`
- `Consulting`
- `Staffing and Recruiting`
- `Oil and Gas`
- `Financial Services`
- `Information Technology and Services`
- `Computer Software`
- `Management Consulting`
- `Civil Engineering`
- `Mechanical or Industrial Engineering`

---

## File Structure

```
Agent/
├── app/
│   ├── main.py          # FastAPI app — routes + approval logic
│   ├── scheduler.py     # Weekly discovery, daily touches, reply monitor
│   ├── apollo.py        # Apollo.io API client
│   ├── ai.py            # Claude API — scoring + message generation
│   ├── gmail.py         # Gmail API — send + monitor
│   ├── db.py            # SQLite database models
│   ├── config.py        # Settings from .env
│   └── templates/       # Jinja2 HTML templates
├── data/                # SQLite DB (auto-created, gitignored)
├── credentials/         # Google OAuth creds (gitignored)
├── .env                 # Your config (gitignored)
├── .env.example         # Template
└── requirements.txt
```

---

## Running in the Background (Mac)

To keep the bot running without a terminal window open:

```bash
nohup uvicorn app.main:app --host localhost --port 8000 > data/app.log 2>&1 &
echo $! > data/app.pid
```

To stop:
```bash
kill $(cat data/app.pid)
```
