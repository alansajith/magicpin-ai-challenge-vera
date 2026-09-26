# Vera merchant assistant

This submission exposes the five required endpoints with a deterministic, grounded composer. It stores the latest version of each pushed context, routes by `trigger.kind`, retrieves the matching category digest item, and only uses prices, dates, metrics, offers, and names present in the request contexts. No external API or LLM call is required, so the bot is fast and repeatable under the judge timeout.

It also supports the optional teardown endpoint and maintains lightweight conversation state for auto-reply detection, opt-outs, intent transitions, and off-topic redirects.

## Run locally

```bash
python3 -m pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

The endpoint is then available at `http://localhost:8080`. For a local simulator run, set `VERA_BOT_URL=http://localhost:8080` and provide the judge key through `GEMINI_API_KEY`; the simulator resets candidate state before a repeatable pass.

## Design tradeoff

The composer uses explicit trigger-family handlers instead of a remote model. Each handler retrieves the relevant category digest item, selects only active merchant offers, adapts to customer consent/language/history, and emits one focused CTA. Replay handling detects canned auto-replies, intent commitments, opt-outs, hostility, and off-topic requests. This sacrifices some open-ended prose variation, but avoids fabricated facts, external-data privacy risk, non-determinism, and latency. Novel trigger kinds fall back to a safe payload-grounded message.
