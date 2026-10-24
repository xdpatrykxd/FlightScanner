"""Google Flights browser search with route- and date-verified fare extraction."""

import asyncio
import base64
import json
import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import os
import csv
from functools import lru_cache
import httpx

from playwright.async_api import async_playwright


BASE_URL = "https://www.google.com/travel/flights?hl=en&curr=EUR"
AIRLINES = (
    "Aegean Airlines", "Aer Lingus", "Air Canada", "Air China", "Air Europa",
    "Air France", "Air India", "Air New Zealand", "Air Tahiti Nui", "Air Transat", "Alaska Airlines",
    "American Airlines", "ANA", "Asiana Airlines", "Austrian Airlines", "Avianca",
    "Azul", "British Airways", "Brussels Airlines", "Cathay Pacific", "China Airlines",
    "China Eastern", "China Southern", "Copa Airlines", "Delta", " easyJet",
    "Egyptair", "Emirates", "Etihad Airways", "EVA Air", "Finnair", "Iberia",
    "ITA Airways", "Icelandair", "Japan Airlines", "JetBlue", "KLM", "Korean Air", "LATAM",
    "LOT Polish Airlines", "Lufthansa", "Malaysia Airlines", "Oman Air", "Norwegian", "Qantas",
    "Qatar Airways", "Ryanair", "SAS", "Singapore Airlines", "Southwest Airlines",
    "SWISS", "TAP Air Portugal", "Thai Airways", "Turkish Airlines", "United",
    "Vietnam Airlines", "Vueling", "Wizz Air",
)
def search_url(config):
    """Return the stable entry point; Google must build its search state in-page."""
    return BASE_URL


async def _click_consent(page):
    if "consent.google." not in page.url and "Before you continue to Google" not in (await _body(page)):
        return
    button = page.get_by_role("button", name="Reject all", exact=True).last
    if await button.count():
        await button.click()
        await page.wait_for_timeout(900)


async def _choose_airport(page, label, code):
    field = page.locator(f'input[aria-label^="{label}"]').first
    await field.wait_for(state="visible", timeout=12000)
    await field.fill(code)
    await page.wait_for_timeout(450)
    options = page.get_by_role("option")
    count = await options.count()
    for i in range(count):
        option = options.nth(i)
        text = (await option.inner_text()).upper()
        if code.upper() in text:
            await option.click()
            return
    # Google's suggestion list is not consistently exposed as role=option.
    suggestion = page.get_by_text(re.compile(rf"\b{re.escape(code)}\b", re.I)).last
    if await suggestion.count():
        await suggestion.click()
        return
    raise RuntimeError(f"Google Flights did not offer airport {code} for {label.lower()}.")


def city_for_airport(code):
    return _airport_cities().get(str(code).upper(), str(code).upper())


@lru_cache(maxsize=1)
def _airport_cities():
    path=os.path.join(os.path.dirname(os.path.dirname(__file__)),"config","airports.csv")
    with open(path,encoding="utf-8",newline="") as source:
        return {row["iata"].strip().upper():row["city"].strip() for row in csv.DictReader(source) if len(row.get("iata","").strip())==3 and row.get("city","").strip()}


async def _choose_city(page, label, city, fallback_code):
    field = page.locator(f'input[aria-label^="{label}"]').first
    await field.wait_for(state="visible", timeout=12000)
    await field.fill(city)
    await page.wait_for_timeout(450)
    options = page.get_by_role("option")
    for index in range(await options.count()):
        option = options.nth(index)
        text = (await option.inner_text()).strip()
        first_line = text.splitlines()[0].strip().casefold() if text else ""
        normalized_first = re.sub(r",.*$", "", first_line).strip()
        if normalized_first == city.strip().casefold() and "airport" not in text.casefold():
            await option.click()
            return
    await _choose_airport(page, label, fallback_code)


def _calendar_label(value):
    selected = date.fromisoformat(value)
    return f"{selected.strftime('%A')}, {selected.strftime('%B')} {selected.day}, {selected.year}"


def _read_varint(data, offset):
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("Google Flights created invalid route data.")


def _write_varint(value):
    out = bytearray()
    while value > 0x7f:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _set_tfs_dates(encoded, departure, returning):
    """Add the selected dates to Google's protobuf route state.

    Google sometimes updates the visible date fields but creates a TFS token
    without those dates. Preserve the airport/place IDs Google selected, then
    add date fields to its outbound and inbound legs before requesting results.
    """
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    data = bytearray()
    offset = 0
    legs = 0
    while offset < len(raw):
        start = offset
        tag, offset = _read_varint(raw, offset)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, offset = _read_varint(raw, offset)
            if field == 2:
                value = 2  # Round trip; this message carries outbound + return legs.
                data.extend(_write_varint(tag))
                data.extend(_write_varint(value))
            else:
                data.extend(raw[start:offset])
        elif wire == 1:
            offset += 8
            data.extend(raw[start:offset])
        elif wire == 2:
            size, content_start = _read_varint(raw, offset)
            content_end = content_start + size
            if content_end > len(raw):
                raise ValueError("Google Flights created incomplete route data.")
            content = raw[content_start:content_end]
            if field == 3:
                flight_date = departure if legs == 0 else returning
                date_field = b"\x12\x0a" + flight_date.encode("ascii")
                content = date_field + content
                legs += 1
                data.extend(_write_varint(tag))
                data.extend(_write_varint(len(content)))
                data.extend(content)
            else:
                data.extend(raw[start:content_end])
            offset = content_end
        elif wire == 5:
            offset += 4
            data.extend(raw[start:offset])
        else:
            raise ValueError("Google Flights created unsupported route data.")
    if legs != 2:
        raise ValueError("Google Flights did not create both legs of the round-trip route.")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


async def _choose_calendar_day(page, label):
    # Google renders a rolling calendar. Advance months until the exact day exists.
    for _ in range(18):
        day = page.get_by_role("button", name=label, exact=True)
        if await day.count():
            await day.first.click()
            return
        next_button = page.get_by_role("button", name=re.compile(r"^(Next|Next month)$", re.I)).last
        if not await next_button.count():
            break
        await next_button.click()
        await page.wait_for_timeout(80)
    raise RuntimeError(f"Google Flights calendar could not select {label}.")


async def _set_dates(page, departure, returning):
    await page.get_by_role("textbox", name="Departure").click()
    await _choose_calendar_day(page, _calendar_label(departure))
    await _choose_calendar_day(page, _calendar_label(returning))
    done = page.get_by_role("button", name=re.compile(r"^Done\.?$", re.I)).last
    if await done.count():
        await done.click()


async def _set_passengers(page, passengers):
    buttons = page.get_by_role("button", name=re.compile(r"passenger", re.I))
    if not await buttons.count():
        return
    await buttons.first.click()
    adults = max(1, min(9, int(passengers or 1)))
    add = page.get_by_role("button", name="Add adult", exact=True)
    for _ in range(max(0, adults - 1)):
        if await add.count():
            await add.click()
    done = page.get_by_role("button", name="Done", exact=True).last
    if await done.count():
        await done.click()


async def _set_cabin(page, cabin):
    desired = {
        "economy": "Economy", "premium_economy": "Premium economy",
        "business": "Business", "first": "First",
    }.get(str(cabin or "economy").lower(), "Economy")
    current = page.get_by_role("button", name=re.compile(r"(Economy|Business|First class)", re.I))
    if await current.count():
        await current.first.click()
        option = page.get_by_text(desired, exact=True).last
        if await option.count():
            await option.click()


def _price(text):
    patterns = (r"€\s*([\d.,]+)", r"([\d.,]+)\s*€", r"EUR\s*([\d.,]+)", r"([\d.,]+)\s*euros?\b")
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).replace(" ", "")
            if "," in value and "." in value:
                decimal = "." if value.rfind(".") > value.rfind(",") else ","
                group = "," if decimal == "." else "."
                value = value.replace(group, "").replace(decimal, ".")
            elif "," in value or "." in value:
                separator = "," if "," in value else "."
                left, right = value.rsplit(separator, 1)
                value = left + right if len(right) == 3 else left + "." + right
            try:
                return float(value)
            except ValueError:
                pass
    return None


def _airline(text):
    matches = []
    for name in AIRLINES:
        cleaned = name.strip()
        match = re.search(rf"(?<!\w){re.escape(cleaned)}(?!\w)", text, re.I)
        if match:
            matches.append((match.start(), cleaned))
    ordered = list(dict.fromkeys(name for _, name in sorted(matches)))
    return ", ".join(ordered) if ordered else "Airline shown by Google Flights"


def _route_terms(config):
    terms = {str(config.get("origin", "")).upper(), str(config.get("destination", "")).upper()}
    for key in ("origin_airport_names", "destination_airport_names"):
        terms.update(str(item).strip().lower() for item in config.get(key, []) if item)
    return {term for term in terms if term}


def _route_is_present(body, config):
    upper = body.upper()
    origins = [str(code).upper() for code in config.get("origin_airports") or [config.get("origin", "")]]
    destinations = [str(code).upper() for code in config.get("destination_airports") or [config.get("destination", "")]]
    origin_city = str(config.get("origin_city", "")).upper()
    destination_city = str(config.get("destination_city", "")).upper()
    origin_present = any(code and re.search(rf"\b{re.escape(code)}\b", upper) for code in origins) or bool(origin_city and origin_city in upper)
    destination_present = any(code and re.search(rf"\b{re.escape(code)}\b", upper) for code in destinations) or bool(destination_city and destination_city in upper)
    return origin_present and destination_present


def _extract_offers(cards, config):
    """Read visible Google fare cards, using their own text and route labels."""
    origin = str(config["origin"]).upper()
    destination = str(config["destination"]).upper()
    offers = []
    seen = set()
    origin_codes = [str(code).upper() for code in config.get("origin_airports") or [origin]]
    destination_codes = [str(code).upper() for code in config.get("destination_airports") or [destination]]
    for raw_card in cards:
        data=raw_card if isinstance(raw_card,dict) else {}
        card = " ".join(str(data.get('text',raw_card)).split())
        actual_origin = next((code for code in origin_codes if re.search(rf"\b{re.escape(code)}\b", card, re.I)), origin)
        actual_destination = next((code for code in destination_codes if re.search(rf"\b{re.escape(code)}\b", card, re.I)), None)
        if actual_destination is None:
            continue
        value = _price(card)
        if value is None:
            continue
        has_flight_signal = bool(re.search(r"\b(?:nonstop|\d+\s+(?:stops?|tussenstops?)|\d+\s+(?:hr|h|uur|min))\b", card, re.I))
        if not has_flight_signal:
            continue
        stops_match = re.search(r"\b(\d+)\s+(?:stops?|tussenstops?)\b", card, re.I)
        stops = int(stops_match.group(1)) if stops_match else 0
        if re.search(r"\b(?:nonstop|rechtstreeks)\b", card, re.I):
            stops = 0
        key = (value, _airline(card), card[:180])
        if key in seen:
            continue
        seen.add(key)
        segments=[]
        for flight_url in data.get('flight_urls',[]):
            itinerary=re.search(r'[?&]itinerary=([^&]+)',flight_url)
            if itinerary:
                segments=[{"from":a,"to":b,"airline_code":air,"flight_number":number,"date":day[:4]+'-'+day[4:6]+'-'+day[6:]}
                          for a,b,air,number,day in re.findall(r'([A-Z]{3})-([A-Z]{3})-([A-Z0-9]{2,3})-(\d+)-(\d{8})',itinerary.group(1))]
                if segments: break
        def label_value(prefix):
            return next((re.sub(r'\.$','',label.split(':',1)[-1].strip()) for label in data.get('time_labels',[]) if label.startswith(prefix)),None)
        leg={"from": actual_origin, "to": actual_destination, "stops": stops, "segments":segments,
             "departure_time":label_value('Departure time'),"arrival_time":label_value('Arrival time'),
             "duration":data.get('duration'),"layovers":data.get('layovers',[])}
        emissions=data.get('emissions')
        offers.append({
            "price": value, "currency": "EUR", "airline": data.get('airline') or _airline(card),
            "itinerary": [leg], "emissions":emissions,
            "source_text": card[:500],
        })
    return offers


async def _body(page):
    try:
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


async def search(config, timeout_seconds=25):
    timeout_ms = max(10000, int(float(timeout_seconds) * 1000))
    cdp_endpoint = os.getenv("GOOGLE_FLIGHTS_CDP_URL", "http://google-browser:9222")
    async with async_playwright() as playwright:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    f"{cdp_endpoint.rstrip('/')}/json/version", headers={"Host": "localhost:9223"}
                )
                response.raise_for_status()
                websocket_url = response.json()["webSocketDebuggerUrl"]
            websocket_parts = urlsplit(websocket_url)
            endpoint_host = urlsplit(cdp_endpoint).netloc
            websocket_url = urlunsplit((websocket_parts.scheme, endpoint_host, websocket_parts.path, websocket_parts.query, websocket_parts.fragment))
            browser = await playwright.chromium.connect_over_cdp(
                websocket_url, timeout=10000, headers={"Host": "localhost:9223"}
            )
        except Exception as exc:
            raise RuntimeError("AirScout cannot connect to its Google browser. Restart the Google sign-in browser service.") from exc
        if not browser.contexts:
            raise RuntimeError("AirScout’s Google browser is not ready. Restart the Google sign-in browser service.")
        context = browser.contexts[0]
        page = await context.new_page()
        page.set_default_timeout(min(timeout_ms, 20000))
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await _click_consent(page)
            if "consent.google." in page.url:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.locator('input[aria-label^="Where from?"]').wait_for(state="visible", timeout=min(timeout_ms, 15000))
            await _choose_city(page, "Where from?", config.get("origin_city") or city_for_airport(config["origin"]), config["origin"])
            await _choose_city(page, "Where to?", config.get("destination_city") or city_for_airport(config["destination"]), config["destination"])
            await _set_passengers(page, config.get("passengers", 1))
            await _set_cabin(page, config.get("cabin", "economy"))
            await _set_dates(page, config["departure_date"], config["return_date"])

            # Google exposes a generic page-level “Search” button after the
            # date-confirmed form action. Select the latter explicitly so the
            # chosen departure and return dates enter the TFS search state.
            search_button = page.get_by_role("button", name=re.compile(r"^Done\. Search for .* flights", re.I)).last
            await search_button.wait_for(state="visible", timeout=10000)
            await search_button.click()

            try:
                await page.wait_for_function("location.search.includes('tfs=')", timeout=10000)
            except Exception as exc:
                raise RuntimeError("Google Flights did not create a search for the selected route and dates.") from exc
            parts = urlsplit(page.url)
            params = dict(parse_qsl(parts.query, keep_blank_values=True))
            try:
                params["tfs"] = _set_tfs_dates(params["tfs"], config["departure_date"], config["return_date"])
            except (KeyError, ValueError) as exc:
                raise RuntimeError(f"Could not add the selected dates to Google Flights’ search: {exc}") from exc
            # tfu represents the form’s landing-page transition, not the actual
            # results search. A dated TFS token is sufficient for the results URL.
            params.pop("tfu", None)
            query = urlencode(params)
            result_url = urlunsplit((parts.scheme, parts.netloc, "/travel/flights/search", query, parts.fragment))
            if result_url != page.url:
                await page.goto(result_url, wait_until="domcontentloaded", timeout=timeout_ms)

            body = ""
            deadline = asyncio.get_running_loop().time() + max(20, float(timeout_seconds))
            while asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(850)
                body = await _body(page)
                low = body.lower()
                if await page.locator("div.mxvQLc").count():
                    break
                finished = any(marker in low for marker in (
                    "no results returned", "no flights found", "couldn't find", "could not find",
                    "search results", "top flights", "best flights", "oops, something went wrong",
                )) and "loading results" not in low
                if finished:
                    break

            if not body:
                body = await _body(page)
            if not _route_is_present(body, config):
                raise RuntimeError("Google Flights opened a page without the selected route; no fares were read.")

            result_markers = ("no results returned", "no flights found", "didn't find any flights",
                              "didn’t find any flights", "couldn't find flights", "could not find flights",
                              "search results", "top flights", "best flights", "oops, something went wrong")

            lowered = body.lower()
            if "oops, something went wrong" in lowered:
                raise RuntimeError("Google Flights returned an error for this route. If it asks you to sign in, open Settings → Google Flights sign-in, then retry.")
            if any(message in lowered for message in (
                "no results returned", "no flights found", "didn't find any flights",
                "didn’t find any flights", "couldn't find flights", "could not find flights",
            )):
                return [], body, page.url

            cards = await page.locator("div.mxvQLc").evaluate_all("""cards => cards.map(card => {
              const attr=(selector,prefix='')=>{const x=card.querySelector(selector);return x?.getAttribute('aria-label')||''};
              const flightUrls=[...new Set([...card.querySelectorAll('[data-travelimpactmodelwebsiteurl]')].map(x=>x.getAttribute('data-travelimpactmodelwebsiteurl')).filter(Boolean))];
              const carrier=card.querySelector('.Ir0Voe .sSHqwe > span:first-child')?.innerText?.trim()||'';
              const emissions=card.querySelector('[data-co2currentflight]');
              const current=Number(emissions?.getAttribute('data-co2currentflight')||0),typical=Number(emissions?.getAttribute('data-co2typical')||0);
              return {text:card.innerText,airline:carrier,time_labels:[...card.querySelectorAll('.Ir0Voe [aria-label^="Departure time:"],.Ir0Voe [aria-label^="Arrival time:"]')].map(x=>x.getAttribute('aria-label')),duration:attr('[aria-label^="Total duration"]'),layovers:[...card.querySelectorAll('[aria-label*="Layover"]')].map(x=>x.getAttribute('aria-label')),flight_urls:flightUrls,emissions:current?{kg:Math.round(current/1000),percent_vs_typical:typical?Math.round((current-typical)/typical*100):0}:null};
            })""")
            offers = _extract_offers(cards, config)
            if not offers:
                if not any(marker in lowered for marker in result_markers):
                    raise RuntimeError("Google Flights did not load search results. Open Settings → Google Flights sign-in, sign in if prompted, and retry.")
                raise RuntimeError("Google Flights loaded the selected route but showed no readable fare cards. Try again later or check the AirScout logs.")
            return offers, body, page.url
        finally:
            await page.close()
