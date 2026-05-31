# newsbrief

Daily geopolitical and macro morning brief via Claude Batch API.

Fetches RSS feeds, Nitter/RSSHub Twitter feeds, and the BCA Iran dashboard
each evening, submits a batch job to Claude, and delivers a structured brief
to you each morning via Apprise (Telegram, Ntfy, Pushover, etc.).

## Setup

### 1. API key

Create an account at https://console.anthropic.com and add credits.
At one call/day with Sonnet 4.6 this costs well under $1/month.

### 2. Apprise notification URL

Configure whichever channel you already use. If you have Apprise running
on your server, you likely already have a URL format you're familiar with.

Common formats:
- Telegram:  `tgram://bottoken/chatid`
  - Create a bot: message @BotFather on Telegram → /newbot
  - Get your chat ID: message @userinfobot
- Ntfy:      `ntfy://your-ntfy-host/newsbrief`
- Pushover:  `pover://userkey@apptoken`

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your API key and Apprise URL
```

### 4. Build

```bash
docker compose build
```

### 5. Test (synchronous, no batch delay)

```bash
docker compose run --rm newsbrief-submit python brief.py run
```

This submits and polls immediately — useful to verify everything works.
Expect ~30s–2min for a full response.

### 6. Schedule

Add to host crontab (`crontab -e`):

```cron
# Submit every evening at 8pm UTC (9pm BST)
0 20 * * * cd /path/to/newsbrief && docker compose run --rm newsbrief-submit

# Collect every morning at 6am UTC (7am BST)
0 6  * * * cd /path/to/newsbrief && docker compose run --rm newsbrief-collect
```

Adjust the submit time based on your timezone and when you want the brief.
The 10-hour window comfortably fits within the Batch API's 24h turnaround.

---

## Customisation

### Adding feeds

Edit `RSS_FEEDS` in `brief.py`. Any Substack, RSS feed, or RSSHub source works.

### Adding Nitter/Twitter feeds

RSSHub is used instead of Nitter for reliability:
```python
{
    "name": "Handle (@username)",
    "url": "https://rsshub.app/twitter/user/username",
    "category": "geo",
}
```

If you self-host RSSHub, replace `rsshub.app` with your instance URL.

### Tuning the prompt

The system prompt and output format are in `brief.py` — `SYSTEM_PROMPT` and
`build_user_prompt()`. The output format (sections, word count, tone) is all
in the prompt and easy to iterate on without touching any other code.

### Adding more web sources

Add entries to `WEB_SOURCES`. Works for any page with a useful meta description.
For pages that need deeper scraping, extend `fetch_web_source()`.

### Switching to synchronous API

Remove the `tools` parameter from the batch payload and change the endpoint
to `https://api.anthropic.com/v1/messages`. Switch `mode_submit` to call
directly and skip the collect step.

---

## File layout

```
newsbrief/
├── brief.py            # Main script
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md

/app/logs/              # Mounted volume
├── newsbrief.log       # Running log
├── batch_state.json    # Pending batch ID (cleared after delivery)
└── briefs/
    └── brief-YYYY-MM-DD.md   # Archive of all delivered briefs
```

Every brief is saved to disk regardless of notification success,
so you never lose one if Apprise has a bad day.
```
