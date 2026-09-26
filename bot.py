"""Vera challenge bot.

This is deliberately dependency-light and deterministic.  The judge supplies all
facts through /v1/context; the composer never invents a price, statistic, source,
date, or competitor.  FastAPI is used only for the HTTP adapter while the
composition logic remains usable as the required ``compose`` function.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


SCOPES = {"category", "merchant", "customer", "trigger"}
START_TIME = time.time()
APP_VERSION = "1.0.0"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _first_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    owner = identity.get("owner_first_name")
    if owner:
        return _text(owner)
    name = _text(identity.get("name"), "there")
    parts = name.replace("'", "").split()
    if parts and parts[0].lower() in {"dr.", "dr", "mr.", "mr", "ms.", "ms", "mrs.", "mrs"}:
        return parts[1] if len(parts) > 1 else name
    return parts[0] if parts else "there"


def _merchant_name(merchant: dict[str, Any]) -> str:
    return _text((merchant.get("identity") or {}).get("name"), "your business")


def _category_slug(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> str:
    return _text(category.get("slug") or merchant.get("category_slug") or (trigger.get("payload") or {}).get("category"), "business")


def _active_offers(merchant: dict[str, Any]) -> list[dict[str, Any]]:
    offers = merchant.get("offers") or []
    return [o for o in offers if _text(o.get("status"), "active").lower() in {"active", "live"}]


def _offer_text(merchant: dict[str, Any], preferred: tuple[str, ...] = ()) -> str:
    offers = _active_offers(merchant)
    if not offers:
        return ""
    if preferred:
        for offer in offers:
            title = _text(offer.get("title"))
            if any(word in title.lower() for word in preferred):
                return title
    return _text(offers[0].get("title"))


def _pct(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return _text(value)
    return f"{n * 100:+.0f}%"


def _number(value: Any) -> str:
    if value is None:
        return ""
    try:
        n = float(value)
        if n.is_integer():
            return f"{int(n):,}"
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _compact(value: Any, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", _text(value)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _date_label(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return raw


def _datetime_label(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p").lstrip("0")
    except ValueError:
        return raw


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    payload = trigger.get("payload") or {}
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("item_id")
    digest = category.get("digest") or []
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    return digest[0] if digest else {}


def _history_last(merchant: dict[str, Any], sender: str | None = None) -> dict[str, Any]:
    history = merchant.get("conversation_history") or []
    for item in reversed(history):
        if sender is None or item.get("from") == sender:
            return item
    return {}


def _category_language(merchant: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    if customer:
        return _text((customer.get("identity") or {}).get("language_pref"), "english").lower()
    langs = (merchant.get("identity") or {}).get("languages") or []
    return ",".join(_text(x).lower() for x in langs)


def _customer_can_contact(customer: Optional[dict[str, Any]]) -> bool:
    if not customer:
        return False
    consent = customer.get("consent") or {}
    scopes = consent.get("scope") or []
    pref = customer.get("preferences") or {}
    return bool(consent.get("opted_in_at") and scopes and pref.get("reminder_opt_in", True) is not False)


def _customer_slot_text(payload: dict[str, Any]) -> str:
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    labels = [_text(s.get("label") or s.get("iso")) for s in slots if isinstance(s, dict)]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return " or ".join(labels[:3])


def _metric_snapshot(merchant: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str, str]:
    metric = _text(payload.get("metric"), "performance").replace("_", " ")
    delta = payload.get("delta_pct")
    if delta is None:
        delta = ((merchant.get("performance") or {}).get("delta_7d") or {}).get(f"{payload.get('metric')}_pct")
    change = _pct(delta) if delta is not None else ""
    current = _number((merchant.get("performance") or {}).get(payload.get("metric")))
    return metric, change, current


def _merchant_greeting(merchant: dict[str, Any]) -> str:
    slug = _text(merchant.get("category_slug"))
    first = _first_name(merchant)
    if slug == "dentists":
        return f"Dr. {first}" if not first.lower().startswith("dr") else first
    return first


def _rationale(kind: str, category: dict[str, Any], merchant: dict[str, Any], customer: Optional[dict[str, Any]], fact: str, cta: str) -> str:
    audience = "customer-facing via the merchant" if customer else "merchant-facing via Vera"
    return f"{kind.replace('_', ' ').capitalize()} message for {_category_slug(category, merchant, {})}; {audience}. Anchored on {fact}. One next step: {cta}."


def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compose one deterministic, grounded message from the four contexts."""
    kind = _text(trigger.get("kind"), "context_update").lower()
    payload = trigger.get("payload") or {}
    slug = _category_slug(category, merchant, trigger)
    name = _merchant_greeting(merchant)
    business = _merchant_name(merchant)
    language = _category_language(merchant, customer)
    send_as = "merchant_on_behalf" if customer else "vera"
    suppression = _text(trigger.get("suppression_key"), _text(trigger.get("id"), f"{kind}:{merchant.get('merchant_id', '')}"))
    cta = "open_ended"
    fact = kind
    body = ""

    # Customer journeys are dispatched first because they must use the customer's
    # relationship, consent, and language rather than the merchant's greeting.
    if customer:
        cname = _text((customer.get("identity") or {}).get("name"), "there")
        customer_lang = language
        if not _customer_can_contact(customer):
            return {
                "body": "",
                "cta": "none",
                "send_as": send_as,
                "suppression_key": suppression,
                "rationale": "Customer outreach suppressed because an explicit opt-in scope was not present.",
            }
        if kind in {"recall_due", "appointment_tomorrow"}:
            slots = _customer_slot_text(payload)
            offer = _offer_text(merchant, ("clean", "check", "consult", "appointment"))
            last_visit = _date_label((customer.get("relationship") or {}).get("last_visit"))
            due = _date_label(payload.get("due_date"))
            service = _text(payload.get("service_due"), "your next visit").replace("_", " ")
            if customer_lang.startswith("hi"):
                lead = f"Hi {cname} 👋 {business} here. Aapki {service} recall window"
                slot_line = f"Apke liye slot{'s' if len(slots.split(' or ')) > 1 else ''}: {slots}." if slots else "Aap apna convenient time bata sakte hain."
            else:
                lead = f"Hi {cname} 👋 {business} here. Your {service} recall window"
                slot_line = f"I have {slots} available." if slots else "Tell us a weekday time that works."
            timing = f" opened{(' for ' + _date_label(due)) if due else ''}"
            history_bit = f" It has been since {last_visit}." if last_visit else ""
            offer_bit = f" {offer}." if offer else ""
            body = f"{lead}{timing}.{history_bit}{offer_bit} {slot_line} Reply with the slot that works, or share another time."
            cta = "multi_choice_slot" if slots else "open_ended"
            fact = f"{kind} trigger and {slots or 'available timing'}"
        elif kind in {"customer_lapsed_hard", "customer_lapsed_soft", "winback_customer", "winback"}:
            days = payload.get("days_since_last_visit")
            focus = _text(payload.get("previous_focus"), "your earlier goal")
            offer = _offer_text(merchant, ("trial", "month", "class", "membership"))
            timing = f"about {days} days" if days is not None else "a little while"
            no_shame = "No pressure — returning should fit your routine."
            offer_bit = f" We have {offer}." if offer else ""
            body = f"Hi {cname} 👋 {_merchant_name(merchant)} here. It’s been {timing}; {no_shame} I remember your focus on {focus}.{offer_bit} Want me to hold one low-commitment slot for you?"
            cta = "binary_yes_no"
            fact = f"{days or 'recent'} days since the last visit and {focus} goal"
        elif kind in {"trial_followup", "wedding_package_followup", "bridal_followup"}:
            slots = _customer_slot_text(payload)
            wedding = _date_label(payload.get("wedding_date"))
            offer = _offer_text(merchant, ("bridal", "skin", "trial", "package"))
            service = "bridal follow-up" if "wedding" in kind or "bridal" in kind else "your trial"
            offer_bit = f" {offer} is available." if offer else ""
            date_bit = f" Your wedding is {wedding}." if wedding else ""
            slot_bit = f" I can hold {slots}." if slots else ""
            body = f"Hi {cname} 💍 {_merchant_name(merchant)} here — following up on {service}.{date_bit}{offer_bit}{slot_bit} Shall I reserve the next step?"
            cta = "binary_yes_no"
            fact = f"{kind} context" + (f" and {wedding} wedding date" if wedding else "")
        elif kind in {"chronic_refill_due", "refill_due"}:
            molecules = payload.get("molecule_list") or (customer.get("relationship") or {}).get("services_received") or []
            molecule_text = ", ".join(_text(x) for x in molecules[:5])
            runout = _date_label(payload.get("stock_runs_out_iso"))
            pref = customer.get("preferences") or {}
            delivery = "your saved delivery address" if pref.get("delivery_address") == "saved" or payload.get("delivery_address_saved") else "pickup or delivery"
            greeting = "Namaste" if customer_lang.startswith("hi") else "Hi"
            body = f"{greeting} {cname}, {_merchant_name(merchant)} here. Your refill list: {molecule_text or 'the medicines in your last order'}; stock is expected to run out{(' on ' + runout) if runout else ' soon'}. We can arrange {delivery}. Shall I prepare it for your confirmation?"
            cta = "binary_yes_no"
            fact = f"refill medicines {molecule_text or 'from the customer record'} and run-out date {runout or 'in the trigger'}"
        else:
            # Safe generic customer fallback: only mention fields explicitly sent.
            body = f"Hi {cname}, {_merchant_name(merchant)} here. I have an update related to your account. Would you like me to share the next step?"
            cta = "open_ended"
            fact = f"customer trigger {kind}"
        return {"body": _compact(body, 900), "cta": cta, "send_as": send_as,
                "suppression_key": suppression, "rationale": _rationale(kind, category, merchant, customer, fact, cta)}

    # Merchant-facing trigger families.
    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        item = _digest_item(category, trigger)
        title = _text(item.get("title"), "a new category item")
        source = _text(item.get("source"))
        summary = _text(item.get("summary"))
        parts = [f"{name}, {source + ' ' if source else ''}has a new item: {title}."]
        if item.get("trial_n") is not None:
            parts.append(f"It covers {_number(item['trial_n'])} participants.")
        if summary:
            parts.append(_compact(summary, 200))
        signals = " ".join(_text(s) for s in (merchant.get("signals") or []))
        if "high_risk" in signals.lower() or (merchant.get("customer_aggregate") or {}).get("high_risk_adult_count"):
            parts.append("That is especially relevant to the high-risk cohort in your records.")
        parts.append("Want me to turn the useful point into one patient-ready post?")
        body = " ".join(parts)
        cta = "open_ended"
        fact = f"digest headline{(' and source ' + source) if source else ''}"
    elif kind in {"regulation_change", "compliance_alert", "compliance"}:
        item = _digest_item(category, trigger)
        title = _text(item.get("title"), "a compliance update")
        source = _text(item.get("source"))
        deadline = _date_label(payload.get("deadline_iso"))
        summary = _text(item.get("summary"))
        action = _text(item.get("actionable"), "review the affected workflow")
        body = f"{name}, compliance check: {title}."
        if deadline:
            body += f" The deadline is {deadline}."
        if summary:
            body += f" {_compact(summary, 220)}"
        body += f" Suggested next step: {action}. Want me to turn that into a short audit checklist?"
        cta = "open_ended"
        fact = f"compliance headline and {source or 'provided'} source"
    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        metric, change, current = _metric_snapshot(merchant, payload)
        peer = (category.get("peer_stats") or {}).get(f"avg_{payload.get('metric')}_30d")
        expected = " The trigger marks this as seasonal, so I would not overreact." if payload.get("is_expected_seasonal") else ""
        peer_bit = f" Peer average is {_number(peer)}." if peer is not None else ""
        current_bit = f" Current {metric}: {current}." if current else ""
        body = f"{name}, your {metric} is {change or 'down'} over {payload.get('window', 'the current window')}.{current_bit}{peer_bit}{expected} Want me to draft one focused recovery/retention action for this window?"
        cta = "open_ended"
        fact = f"{metric} change {change or 'from the trigger'}"
    elif kind in {"perf_spike", "milestone_reached"}:
        metric, change, current = _metric_snapshot(merchant, payload)
        driver = _text(payload.get("likely_driver"), "the recent activity")
        milestone = payload.get("milestone_value")
        if kind == "milestone_reached" or milestone is not None:
            value = _number(payload.get("value_now"))
            body = f"{name}, you are at {value or 'the latest'} {metric}; the next milestone is {milestone or 'in view'}. Nice moment to make the proof visible. Want me to draft the GBP post?"
            fact = f"milestone value {value or milestone}"
        else:
            body = f"{name}, {metric} is up {change or 'in the latest window'}; the likely driver is {driver}. Want me to turn that winning signal into one repeatable post?"
            fact = f"{metric} movement {change or 'from the trigger'}"
        cta = "open_ended"
    elif kind in {"renewal_due"}:
        days = payload.get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
        plan = _text(payload.get("plan"), _text((merchant.get("subscription") or {}).get("plan"), "current plan"))
        amount = payload.get("renewal_amount")
        amount_bit = f" at ₹{_number(amount)}" if amount is not None else ""
        body = f"{name}, your {plan} subscription has {days if days is not None else 'limited'} days left{amount_bit}. If you want continuity, shall I prepare the renewal link/checklist?"
        cta = "binary_yes_no"
        fact = f"subscription status with {days if days is not None else 'the supplied'} days remaining"
    elif kind in {"festival_upcoming", "festival", "category_seasonal"}:
        if kind == "category_seasonal":
            trends = payload.get("trends") or []
            trend = _text(trends[0]) if trends else "the supplied seasonal shift"
            body = f"{name}, the seasonal signal to act on is {trend}. For {_category_slug(category, merchant, trigger)}, that is a better reason to adjust the shelf/content mix than to run a generic discount. Want me to draft the update?"
            fact = f"seasonal signal {trend}"
        else:
            festival = _text(payload.get("festival"), "the upcoming festival")
            date = _date_label(payload.get("date"))
            offer = _offer_text(merchant)
            offer_bit = f" Your live offer is {offer}." if offer else ""
            body = f"{name}, {festival} is on {date or 'the date in the trigger'}{'.' if date else ''}{offer_bit} Want me to turn the offer you already have into one timely local post?"
            fact = f"{festival} date {date or 'from the trigger'}"
        cta = "open_ended"
    elif kind in {"ipl_match_today", "match_today"}:
        match = _text(payload.get("match"), "today's match")
        venue = _text(payload.get("venue"))
        when = _datetime_label(payload.get("match_time_iso"))
        offer = _offer_text(merchant, ("pizza", "combo", "delivery"))
        channel = "delivery-first" if payload.get("is_weeknight") is False else "match-night"
        offer_bit = f" Use your active {offer} as the anchor." if offer else ""
        venue_bit = f" at {venue}" if venue else ""
        body = f"Quick heads-up {name}: {match}{venue_bit} is scheduled for {when or 'the supplied time'}. I would test a {channel} angle rather than a generic promo.{offer_bit} Want me to draft the customer-facing copy?"
        cta = "open_ended"
        fact = f"match {match} and time {when or 'from the trigger'}"
    elif kind in {"review_theme_emerged", "review_theme"}:
        theme = _text(payload.get("theme"), "the review theme")
        count = payload.get("occurrences_30d")
        quote = _text(payload.get("common_quote"))
        quote_bit = f' One customer wrote, "{_compact(quote, 120)}".' if quote else ""
        body = f"{name}, {theme.replace('_', ' ')} has appeared {count or 'multiple times'} in the last 30 days and is {payload.get('trend', 'worth addressing')}.{quote_bit} Want me to draft a reply/process fix for this one theme?"
        cta = "open_ended"
        fact = f"{count or 'repeated'} reviews mentioning {theme}"
    elif kind in {"active_planning_intent", "planning_intent"}:
        last = _history_last(merchant, "merchant")
        last_msg = _text(payload.get("merchant_last_message"), _text(last.get("body")))
        topic = _text(payload.get("intent_topic"), "the idea you raised").replace("_", " ")
        offer = _offer_text(merchant)
        offer_bit = f" Your live offer is {offer}." if offer else ""
        body = f"{name}, picking up your note on {topic}: “{_compact(last_msg, 180)}”{offer_bit} I can draft a first version using only the details already on file. Shall I make it now?"
        cta = "binary_yes_no"
        fact = f"the merchant's stated planning intent: {topic}"
    elif kind in {"supply_alert", "supply_recall"}:
        molecule = _text(payload.get("molecule"), "the affected medicine")
        batches = ", ".join(_text(x) for x in (payload.get("affected_batches") or [])[:5])
        total = (merchant.get("customer_aggregate") or {}).get("chronic_rx_customers")
        total_bit = f" You have {total} chronic-Rx customers in the supplied aggregate." if total is not None else ""
        body = f"{name}, supply alert for {molecule}: batches {batches or 'listed in the trigger'} from {_text(payload.get('manufacturer'), 'the named manufacturer')}.{total_bit} Please hold affected stock and verify replacements. Want me to draft the customer notice + pickup checklist?"
        cta = "binary_yes_no"
        fact = f"{molecule} batch alert {batches or 'from the trigger'}"
    elif kind in {"gbp_unverified", "profile_unverified"}:
        path = _text(payload.get("verification_path"), "the supplied verification path")
        uplift = payload.get("estimated_uplift_pct")
        uplift_bit = f" The supplied estimate is {_pct(uplift)}." if uplift is not None else ""
        body = f"{name}, your Google Business Profile is still unverified; the available route is {path}.{uplift_bit} Want me to walk you through the next verification step?"
        cta = "open_ended"
        fact = f"verified={merchant.get('identity', {}).get('verified')} and path {path}"
    elif kind in {"cde_opportunity", "webinar"}:
        item = _digest_item(category, trigger)
        title = _text(item.get("title"), "the supplied professional-learning opportunity")
        date = _date_label(item.get("date"))
        credits = payload.get("credits", item.get("credits"))
        fee = _text(payload.get("fee"), _text(item.get("fee")))
        detail = f"{date}; " if date else ""
        if credits is not None:
            detail += f"{credits} credits; "
        if fee:
            detail += fee
        body = f"{name}, {title}{(' (' + detail.rstrip('; ') + ')' if detail else '')}. Want me to save the registration details for you?"
        cta = "open_ended"
        fact = f"opportunity title {title}"
    elif kind in {"competitor_opened", "competitor_alert"}:
        competitor = _text(payload.get("competitor_name"), "a nearby competitor")
        distance = _text(payload.get("distance_km"))
        their_offer = _text(payload.get("their_offer"))
        offer_bit = f" Their listed offer is {their_offer}." if their_offer else ""
        body = f"{name}, {competitor} opened {distance + ' km' if distance else 'nearby'}.{offer_bit} This is a useful prompt to sharpen your own listing, not to copy theirs. Want me to draft a comparison-led GBP update using your live offer?"
        cta = "open_ended"
        fact = f"competitor {competitor} at {distance or 'the supplied distance'}"
    elif kind in {"curious_ask_due", "scheduled_recurring"}:
        last = _history_last(merchant)
        recent = _compact(_text(last.get("body")))
        recent_bit = f" Your last Vera note was about: {recent}." if recent else ""
        body = f"Hi {name}! Quick operator question: what service or product has been asked for most this week at {business}?{recent_bit} Reply with just the name and I’ll turn it into one useful customer-ready draft."
        cta = "open_ended"
        fact = "the scheduled curiosity prompt plus the merchant identity"
    elif kind in {"winback_eligible", "dormant_with_vera"}:
        days = payload.get("days_since_last_merchant_message", payload.get("days_since_expiry"))
        lapsed = (merchant.get("customer_aggregate") or {}).get("lapsed_90d_plus", (merchant.get("customer_aggregate") or {}).get("lapsed_180d_plus"))
        lapsed_bit = f" There are {_number(lapsed)} lapsed customers in the supplied aggregate." if lapsed is not None else ""
        body = f"{name}, it has been {days if days is not None else 'a while'} days since the last useful touch.{lapsed_bit} Rather than send a generic promo, want me to draft one win-back message around a real service you currently offer?"
        cta = "open_ended"
        fact = f"{days if days is not None else 'recent'}-day inactivity signal"
    else:
        # Generic fallback still names the trigger and one payload fact, making
        # novel injected trigger kinds useful without pretending to understand them.
        facts = [(k, v) for k, v in payload.items() if isinstance(v, (str, int, float, bool)) and v not in ("", None)]
        key, value = facts[0] if facts else ("kind", kind)
        body = f"{name}, a new {kind.replace('_', ' ')} signal needs attention: {key.replace('_', ' ')} is {_text(value)}. Want me to turn this into one concrete next step?"
        cta = "open_ended"
        fact = f"trigger kind {kind} and payload field {key}"

    # Category voice guardrails are applied as a final deterministic pass.
    taboo = (category.get("voice") or {}).get("vocab_taboo") or (category.get("voice") or {}).get("taboos") or []
    for word in taboo:
        # We never deliberately use taboos; this protects against a supplied
        # quote/summary containing one of them being repeated verbatim.
        if word and re.search(rf"\b{re.escape(_text(word))}\b", body, re.I):
            body = re.sub(rf"\b{re.escape(_text(word))}\b", "the relevant outcome", body, flags=re.I)

    return {"body": _compact(body, 900), "cta": cta, "send_as": send_as,
            "suppression_key": suppression, "rationale": _rationale(kind, category, merchant, customer, fact, cta)}


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.sent_suppressions: set[str] = set()
        self.auto_reply_counts: dict[tuple[str, str], int] = {}

    def put(self, scope: str, context_id: str, version: int, payload: dict[str, Any]) -> tuple[bool, int | None]:
        with self._lock:
            key = (scope, context_id)
            current = self.contexts.get(key)
            if current and current["version"] >= version:
                return False, current["version"]
            self.contexts[key] = {"version": version, "payload": payload}
            return True, None

    def get(self, scope: str, context_id: str | None) -> dict[str, Any] | None:
        if not context_id:
            return None
        with self._lock:
            item = self.contexts.get((scope, context_id))
            return item.get("payload") if item else None

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts = {scope: 0 for scope in SCOPES}
            for scope, _ in self.contexts:
                counts[scope] += 1
            return counts


store = ContextStore()
app = FastAPI(title="Vera Merchant Assistant", version=APP_VERSION)


@app.get("/v1/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
async def metadata() -> dict[str, Any]:
    return {
        "team_name": "Vera Grounded Composer",
        "team_members": ["OpenAI Codex"],
        "model": "deterministic-rule-composer",
        "approach": "grounded trigger dispatch with category-specific templates, versioned context store, and replay-safe conversation handling",
        "contact_email": "team@example.com",
        "version": APP_VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"accepted": False, "reason": "invalid_json", "details": "Request body must be JSON"}, status_code=400)
    scope = data.get("scope") if isinstance(data, dict) else None
    context_id = data.get("context_id") if isinstance(data, dict) else None
    version = data.get("version") if isinstance(data, dict) else None
    payload = data.get("payload") if isinstance(data, dict) else None
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": "scope must be category, merchant, customer, or trigger"}, status_code=400)
    if not isinstance(context_id, str) or not context_id:
        return JSONResponse({"accepted": False, "reason": "invalid_context_id", "details": "context_id is required"}, status_code=400)
    if not isinstance(version, int) or version < 1:
        return JSONResponse({"accepted": False, "reason": "invalid_version", "details": "version must be a positive integer"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"}, status_code=400)
    accepted, current = store.put(scope, context_id, version, payload)
    if not accepted:
        return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": current}, status_code=409)
    return JSONResponse({"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": datetime.now(timezone.utc).isoformat()})


def _context_for_trigger(trigger_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    trigger = store.get("trigger", trigger_id)
    if not trigger:
        return None, None, None, None
    merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
    merchant = store.get("merchant", merchant_id)
    if not merchant:
        return trigger, None, None, None
    category = store.get("category", merchant.get("category_slug"))
    customer_id = trigger.get("customer_id") or (trigger.get("payload") or {}).get("customer_id")
    customer = store.get("customer", customer_id)
    return trigger, merchant, category, customer


@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"actions": []})
    available = data.get("available_triggers") if isinstance(data, dict) else []
    if not isinstance(available, list):
        return JSONResponse({"actions": []})
    actions: list[dict[str, Any]] = []
    for trigger_id in available[:20]:
        if not isinstance(trigger_id, str):
            continue
        trigger, merchant, category, customer = _context_for_trigger(trigger_id)
        if not (trigger and merchant and category):
            continue
        if trigger.get("scope") == "customer" and not customer:
            # Do not downgrade a customer trigger into a merchant broadcast
            # merely because its customer context has not arrived yet.
            continue
        suppression = _text(trigger.get("suppression_key"), trigger_id)
        if suppression in store.sent_suppressions:
            continue
        result = compose(category, merchant, trigger, customer)
        if not result.get("body"):
            continue
        merchant_id = _text(trigger.get("merchant_id"), merchant.get("merchant_id"))
        customer_id = trigger.get("customer_id") or (trigger.get("payload") or {}).get("customer_id")
        audience = "customer" if customer_id else "merchant"
        conversation_id = f"conv_{merchant_id}_{customer_id or 'merchant'}_{trigger_id}"
        store.sent_suppressions.add(suppression)
        store.conversations[conversation_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger": trigger,
            "category": category,
            "merchant": merchant,
            "customer": customer,
            "history": [{"from": "bot", "body": result["body"]}],
            "ended": False,
            "auto_reply_count": 0,
        }
        item = _digest_item(category, trigger)
        params = [_text((customer or {}).get("identity", {}).get("name") if customer else _merchant_greeting(merchant))]
        if item.get("source") and _text(trigger.get("kind")) in {"research_digest", "research_digest_release", "category_research_digest_release", "regulation_change", "compliance_alert", "compliance", "cde_opportunity", "webinar"}:
            params.append(_text(item.get("source")))
        params.append(result["body"])
        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"{audience}_{_text(trigger.get('kind'), 'update')}_v1",
            "template_params": params[:5],
            **result,
        })
    return JSONResponse({"actions": actions})


def _is_auto_reply(message: str) -> bool:
    text = message.lower()
    markers = ("thank you for contacting", "thanks for contacting", "team will respond", "automated reply", "business hours", "we will get back")
    return any(marker in text for marker in markers) and len(text) < 400


def _is_opt_out(message: str) -> bool:
    text = message.lower()
    return bool(re.search(r"\b(stop|unsubscribe|do not message|don't message|not interested|remove me|no thanks)\b", text))


def _is_intent_commitment(message: str) -> bool:
    text = message.lower()
    return bool(re.search(r"\b(let's do it|lets do it|go ahead|yes do it|do it|sign me up|i want to join|confirm|proceed|okay send|ok send)\b", text))


def _is_off_topic(message: str) -> bool:
    text = message.lower()
    return any(term in text for term in ("gst", "tax filing", "unrelated", "can you also", "politics", "crypto"))


@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"action": "wait", "wait_seconds": 1800, "rationale": "Malformed reply payload; waiting for a valid turn."})
    conversation_id = _text(data.get("conversation_id"))
    message = _text(data.get("message"))
    state = store.conversations.get(conversation_id)
    if not state:
        # Replay harnesses may start a fresh conversation without an earlier tick.
        state = {"history": [], "auto_reply_count": 0, "ended": False}
        store.conversations[conversation_id] = state
    state["history"].append({"from": data.get("from_role", "merchant"), "body": message})
    if state.get("ended"):
        return JSONResponse({"action": "end", "rationale": "Conversation was already closed; no further messages will be sent."})
    if _is_opt_out(message):
        state["ended"] = True
        return JSONResponse({"action": "end", "rationale": "Explicit opt-out detected; closing and suppressing this conversation."})
    if _is_auto_reply(message):
        merchant_key = _text(data.get("merchant_id"), "unknown")
        fingerprint = re.sub(r"\s+", " ", message.lower()).strip()
        global_key = (merchant_key, fingerprint)
        store.auto_reply_counts[global_key] = store.auto_reply_counts.get(global_key, 0) + 1
        state["auto_reply_count"] = max(state.get("auto_reply_count", 0) + 1, store.auto_reply_counts[global_key])
        count = state["auto_reply_count"]
        if count >= 3:
            state["ended"] = True
            return JSONResponse({"action": "end", "rationale": "Repeated canned auto-reply three times; no human engagement signal, so the conversation is closed."})
        if count == 2:
            return JSONResponse({"action": "wait", "wait_seconds": 86400, "rationale": "The same canned auto-reply repeated; backing off 24 hours for the owner."})
        return JSONResponse({"action": "wait", "wait_seconds": 14400, "rationale": "Detected a canned business auto-reply; waiting for the owner instead of burning another turn."})
    state["auto_reply_count"] = 0
    if _is_off_topic(message):
        return JSONResponse({"action": "send", "body": "I can help with the merchant-growth item in this thread, but not that unrelated request. Coming back to the original next step — shall I prepare the draft?", "cta": "open_ended", "rationale": "Politely declined the off-mission request and redirected to the active trigger."})
    if _is_intent_commitment(message):
        context = state.get("category"), state.get("merchant"), state.get("trigger"), state.get("customer")
        if all(context):
            category, merchant, trigger, customer = context
            kind = _text(trigger.get("kind"), "the request").replace("_", " ")
            offer = _offer_text(merchant)
            scope = "the selected customer group" if customer else "the requested merchant action"
            body = f"Great — I’ll move this forward now for {scope}." + (f" I’ll keep {offer} as the real offer anchor." if offer else "") + " I’ll bring back one reviewable draft next."
            return JSONResponse({"action": "send", "body": body, "cta": "binary_confirm_cancel", "rationale": f"Explicit commitment detected after the {kind} prompt; switched from qualification to execution."})
        return JSONResponse({"action": "send", "body": "Great — I’ll move this forward now and bring back one reviewable draft next.", "cta": "binary_confirm_cancel", "rationale": "Explicit commitment detected; switched directly to action."})
    # For a normal question, reuse the original four-context composition only
    # when it produces a distinct, useful follow-up; otherwise acknowledge and
    # ask one focused question rather than hallucinating an answer.
    history = state.get("history") or []
    previous = next((x.get("body", "") for x in reversed(history[:-1]) if x.get("from") == "bot"), "")
    if message.strip().endswith("?") or any(word in message.lower() for word in ("how", "what", "which", "price", "when")):
        return JSONResponse({"action": "send", "body": "Good question. I’ll keep this to the details already in your context. Which single outcome matters most here: more bookings, a profile update, or a customer-ready message?", "cta": "open_ended", "rationale": "Answered with a constrained clarification so the next draft stays grounded and one-step."})
    return JSONResponse({"action": "send", "body": "Got it. I’ll keep the next step focused and grounded in the details already shared. Shall I prepare the draft?", "cta": "open_ended", "rationale": "Acknowledged the reply and offered one low-friction next step without inventing new facts."})


@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    with store._lock:
        store.contexts.clear()
        store.conversations.clear()
        store.sent_suppressions.clear()
        store.auto_reply_counts.clear()
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=8080)
