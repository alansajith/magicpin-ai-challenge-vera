# Vera merchant assistant

This submission exposes the five required endpoints with a deterministic, grounded composer. It stores the latest version of each pushed context, routes by `trigger.kind`, retrieves the matching category digest item, and only uses prices, dates, metrics, offers, and names present in the request contexts. No external API or LLM call is required, so the bot is fast and repeatable under the judge timeout.

It also supports the optional teardown endpoint and maintains lightweight conversation state for auto-reply detection, opt-outs, intent transitions, and off-topic redirects.

## Run locally

```bash
python3 -m pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

The endpoint is then available at `http://localhost:8080`. The provided simulator can be configured with `BOT_URL=http://localhost:8080`.

## Design tradeoff

The composer uses explicit trigger-family handlers instead of a remote model. This sacrifices some open-ended prose variation, but avoids fabricated facts, external-data privacy risk, non-determinism, and latency. Novel trigger kinds fall back to a safe payload-grounded message.
