"""Comprehensive, zero-network-overhead metadata extractor for Google Maps place pages."""

import re
from typing import Dict, Any, Optional, List
from playwright.async_api import Page

PERSIAN_TO_ENGLISH_DIGITS = {
    '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
    '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
    '٫': '.', '،': ','
}

def normalize_digits(text: Optional[str]) -> str:
    if not text:
        return ""
    res = text
    for p, e in PERSIAN_TO_ENGLISH_DIGITS.items():
        res = res.replace(p, e)
    return res.strip()

async def extract_place_metadata(page: Page) -> Dict[str, Any]:
    """
    Extracts complete place metadata directly from the loaded place DOM.
    Zero additional HTTP requests, zero tab switching risks.
    """
    raw = await page.evaluate('''() => {
        const getText = (sel, parent = document) => {
            const el = parent.querySelector(sel);
            return el ? el.innerText.trim() : null;
        };

        const getAttr = (sel, attr, parent = document) => {
            const el = parent.querySelector(sel);
            return el ? el.getAttribute(attr) : null;
        };

        // 1. Title & Category
        const title = getText("h1.DUwDvf, h1");
        const category = getText("button[jsaction*='pane.rating.category'], .DkEaL");

        // 2. Rating & Reviews count
        let rating = null;
        let reviewsCount = null;
        const ratingEl = document.querySelector("div.F7nice span[aria-hidden='true'], span.ceNzKf");
        if (ratingEl) rating = ratingEl.innerText.trim();

        const allSpans = document.querySelectorAll("div.F7nice span");
        for (let s of allSpans) {
            const txt = s.innerText.trim();
            if (txt.includes('(') || txt.includes(')')) {
                reviewsCount = txt.replace(/[()]/g, '').trim();
                break;
            }
        }
        if (!reviewsCount) {
            const revAria = document.querySelector("div.F7nice span[aria-label]");
            if (revAria) reviewsCount = revAria.getAttribute("aria-label");
        }

        // 3. Status checks (Permanently / Temporarily Closed)
        const bodyText = document.body.innerText;
        const isPermClosed = bodyText.includes("بسته شده به صورت دائمی") || bodyText.includes("Permanently closed");
        const isTempClosed = bodyText.includes("بسته شده موقتاً") || bodyText.includes("Temporarily closed");

        // 4. Data-item-id elements (Address, Phone, Authority, Plus Code, Merchant, Menu, LocatedIn)
        const dataItems = {};
        document.querySelectorAll('[data-item-id]').forEach(el => {
            const itemId = el.getAttribute('data-item-id') || '';
            const textEl = el.querySelector('.Io6YTe, .fontBodyMedium') || el;
            const linkEl = el.tagName === 'A' ? el : el.querySelector('a');
            dataItems[itemId] = {
                text: textEl.innerText.trim(),
                href: linkEl ? linkEl.getAttribute('href') : null
            };
        });

        // 5. Price Level
        let priceLevel = null;
        const priceEl = document.querySelector("span[aria-label*='قیمت'], span[aria-label*='Price'], span[aria-label*='هزینه']");
        if (priceEl) priceLevel = priceEl.innerText.trim();

        // 6. Opening hours table
        const hoursSchedule = [];
        const hourRows = document.querySelectorAll("table.eKjhZj tr, table.mxowUb tr, table.OqCZI tr");
        hourRows.forEach(r => {
            const dayEl = r.querySelector("td:first-child, td.ylH6lf");
            const valEl = r.querySelector("td:last-child, td.mxowUb");
            if (dayEl && valEl) {
                hoursSchedule.push({
                    day: dayEl.innerText.trim(),
                    hours: valEl.innerText.trim()
                });
            }
        });

        // 7. Popular Times Histogram (if visible on overview)
        const popularTimes = [];
        const barEls = document.querySelectorAll("div[aria-label*='busy at'], div[aria-label*='شلوغ']");
        barEls.forEach(b => {
            const label = b.getAttribute('aria-label');
            if (label) popularTimes.push(label);
        });

        return {
            title,
            category,
            rating,
            reviewsCount,
            isPermClosed,
            isTempClosed,
            priceLevel,
            dataItems,
            hoursSchedule,
            popularTimes
        };
    }''')

    data_items = raw.get("dataItems", {})

    # Extract Address
    address = None
    for k in data_items:
        if k == "address" or "address" in k:
            address = data_items[k]["text"]
            # Clean up leading icon character if present
            address = re.sub(r"^[\ue000-\uf8ff\s]+", "", address).strip()
            break

    # Extract Phone
    phone = None
    phone_unformatted = None
    for k in data_items:
        if "phone" in k:
            phone = data_items[k]["text"]
            phone = re.sub(r"^[\ue000-\uf8ff\s]+", "", phone).strip()
            if "phone:tel:" in k:
                phone_unformatted = k.split("phone:tel:")[-1].strip()
            break

    # Extract Website
    website = None
    for k in data_items:
        if k == "authority" or "authority" in k:
            website = data_items[k]["href"] or data_items[k]["text"]
            if website and "/url?q=" in website:
                # Decode Google redirect
                m = re.search(r"[?&]q=([^&]+)", website)
                if m:
                    from urllib.parse import unquote
                    website = unquote(m.group(1))
            break

    # Extract Plus Code
    plus_code = None
    for k in data_items:
        if k == "oloc" or "oloc" in k:
            plus_code = data_items[k]["text"]
            plus_code = re.sub(r"^[\ue000-\uf8ff\s]+", "", plus_code).strip()
            break

    # Extract Located In
    located_in = None
    for k in data_items:
        if "locatedin" in k:
            located_in = data_items[k]["text"]
            located_in = re.sub(r"^[\ue000-\uf8ff\s]+", "", located_in).strip()
            break

    # Extract Menu URL
    menu_url = None
    for k in data_items:
        if "menu" in k:
            menu_url = data_items[k]["href"] or data_items[k]["text"]
            break

    # Claimed status (if 'merchant' button exists and invites to claim, business is NOT claimed)
    is_claimed = True
    for k in data_items:
        if "merchant" in k:
            txt = data_items[k]["text"]
            if "ادعای مالکیت" in txt or "Claim this business" in txt or "مالک این کسب" in txt:
                is_claimed = False
            break

    # Parse rating float
    raw_rating = raw.get("rating")
    rating_val = None
    if raw_rating:
        norm_r = normalize_digits(raw_rating)
        m = re.search(r"(\d+(?:\.\d+)?)", norm_r)
        if m:
            try:
                rating_val = float(m.group(1))
            except ValueError:
                pass

    # Parse reviews count integer
    raw_reviews = raw.get("reviewsCount")
    reviews_count_val = None
    if raw_reviews:
        norm_rev = normalize_digits(raw_reviews)
        # Extract digits after removing commas
        clean_rev = re.sub(r"[^\d]", "", norm_rev)
        if clean_rev:
            try:
                reviews_count_val = int(clean_rev)
            except ValueError:
                pass

    return {
        "title": raw.get("title"),
        "category_name": raw.get("category"),
        "total_score": rating_val,
        "reviews_count": reviews_count_val,
        "address": address,
        "phone": phone,
        "phone_unformatted": phone_unformatted,
        "website": website,
        "plus_code": plus_code,
        "is_claimed": is_claimed,
        "is_permanently_closed": raw.get("isPermClosed", False),
        "is_temporarily_closed": raw.get("isTempClosed", False),
        "price_level": raw.get("priceLevel"),
        "located_in": located_in,
        "menu_url": menu_url,
        "opening_hours": raw.get("hoursSchedule", []),
        "popular_times": raw.get("popularTimes", [])
    }
