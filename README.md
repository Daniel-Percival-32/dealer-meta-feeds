# Dealer Meta vehicle feeds

Public vehicle catalogue feeds for Meta (Facebook and Instagram) automotive ads. Each one is built every hour from the dealer's own stock system.

| Dealer | Source | Feed address (what Meta collects) |
|---|---|---|
| Chestnut WAV Centre | CarDealer5 stock API | `https://raw.githubusercontent.com/Daniel-Percival-32/dealer-meta-feeds/feed/chestnut-wav-centre.csv` |

## How it works

- `.github/workflows/hourly-tick.yml` is the clock. Each run starts a feed build if none has run in the last 45 minutes, waits about 55 minutes in the `hourly-wait` environment, then starts the next run. GitHub's own cron is best-effort and skipped most hourly slots here, so it is only used to restart the clock if the chain ever breaks.
- `.github/workflows/build-feeds.yml` builds the feed. The clock starts it, its own cron is a spare, and it can also be started by hand.
  - It reads the dealer's for-sale stock with `build_feed.py` and writes Meta's vehicle feed format.
  - It publishes the file as the single commit on the `feed` branch. No history is kept, so the branch only ever holds the current stock.
- Each vehicle's web page is taken from the dealer's own sitemap. Photos must come from the stock system's image servers.
- A file only replaces the last one after a complete stock read. A run that would remove more than half the vehicles is refused unless started by hand with "allow drop". When a run is refused or fails, the last good file stays live.
- The stock API key lives in this repository's Actions secrets, never in the code.
- Only public advert data is written. Purchase prices and anything else internal are never included.
