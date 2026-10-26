# User manual

## Install the browser extension (Chrome, Edge or Brave)

The extension is loaded "unpacked" from your copy of the repository. It is not on the Chrome Web
Store.

1. Open `chrome://extensions` (Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension` folder inside your `couponscraper` folder.
4. Click the extension's **Details → Extension options**. Paste the token from
   `couponhunter token`, keep the backend as `http://127.0.0.1:8765`, then **Save** and
   **Test connection**.
5. Optional: tick "Automatically save price & offers" if you're comfortable with automatic
   capture (see docs/06 §Residual risks). Otherwise nothing is sent until you click Save.
6. Optional but recommended: tick **"Record prices of my watched products wherever they appear"**.
   Search results, deals, wishlist and cart pages you browse then keep your watched products'
   prices fresh, which is what makes price alerts work. Only watched products are recorded, at most
   hourly, and never on checkout, sign-in, account or payment pages.

Permissions it asks for: storage (its own settings) and access to `127.0.0.1`/`localhost` (your
backend). It runs only on amazon.in product pages, and reads nothing from checkout, your account
or other sites.

After `git pull`, click the reload icon on the extension card.

## Fastest setup (Windows)

```powershell
git clone https://github.com/ciaphuscain/couponscraper.git; cd couponscraper
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -AutoStart
```
It installs everything into `.venv`, creates your data folder, starts CouponHunter at every logon,
runs `couponhunter doctor` and prints your token plus the extension steps. Re-running it is safe.
Run `couponhunter doctor` any time something seems off: each problem comes with the exact fix.

## On your phone (Telegram companion)

After setting up Telegram (docs/09 §4), keep `couponhunter serve` running and talk to your bot:

| You do | It does |
|---|---|
| In the Amazon app: **Share → Telegram → your bot** | Starts tracking the product and replies with what it knows |
| `/price B0XXXXXXXX 1299` | Records the price you see in the app, replies with the best plan |
| `/offer B0XXXXXXXX 10% Instant Discount up to INR 1250 on HDFC Bank Credit Card` | Adds the offer exactly as the app shows it |
| `/best <link or ASIN>` | Lowest realistic price, verdict (buy now / wait), steps, offer numbers |
| `/applied 12` / `/failed 12` | Records whether offer #12 worked (numbers appear in replies and alerts) |
| `/target B0XXXXXXXX 9000` · `/list` · `/stop` · `/cards` · `/status` | Self-explanatory |

The bot ignores messages from anyone except your own chat.

## Everyday use

### On a product page

A panel appears bottom-right.

* **Not tracked yet** → click **Track**. This saves the price and offers shown and adds the product
  to your watchlist. **Save price & offers** does the same without watching.
* **Buy now / Wait / Fair price** is the verdict from your observed price history (it names its
  evidence, e.g. "it was ₹9,400 on 27 Sep; 41 observations over 40 days"), your target, and
  offers about to expire.
* **Lowest realistic price** is what you'd pay at checkout using offers whose rules are fully
  known, with the payment method to use and a probability that every offer applies.
* **Possibly ₹X if…** is a cheaper combination that depends on something unconfirmed (e.g.
  whether the coupon and bank offer combine). Try it at checkout; if it works, confirm the rule
  in Settings → stacking rules.
* **+ up to ₹Y cashback later** is never included in the price. It's paid later, if at all.
* Per offer: **Copy code**, **Open offer page** (official sites only), and **Applied ✓ /
  Didn't apply ✗**. Your feedback raises or lowers that offer's confidence.

### Dashboard (http://127.0.0.1:8765)

| Page | Use it to |
|---|---|
| Overview | See what's actionable now, total available savings, realised savings, pending cashback, and source problems |
| Watchlist | Add products by URL/ASIN, set a target price, see the confirmed and possible best |
| Product page | Full answer, every feasible combination, offers that don't apply and why, price history verdict, record a price or a purchase |
| Offers | Browse offers with state (active/stale/expired), source trust, confidence; add an offer you've seen; mark one invalid |
| Savings & history | Purchases, cashback ledger (move records along pending → confirmed → paid), redemption attempts |
| Cards | Which of your cards wins on most watched products, and which card you lack would have saved more (only counting confirmed rules) |
| Health | Source status and errors, recent runs, scheduler jobs, alert outbox |
| Settings | Prime/new-customer status, your payment methods, alert thresholds, quiet hours, stacking rules you've confirmed |

### Adding offers yourself

Paste the offer text exactly as shown (Offers → "Add an offer you've seen"). Say where you saw it:
"on amazon.in or the bank's own site" makes it *official*, anything else stays *unverified*.
For bulk entry, use a JSON/CSV file and `couponhunter import file.json --evidence official_page`
(format in `couponhunter/sources/file_import.py`; example in `fixtures/demo_offers.json`).

### Offers from your email

Save promotional emails (Outlook/Gmail "Download message" → `.eml`) into a folder, or export a
label with Google Takeout (`.mbox`), then:

```powershell
couponhunter import-email "$HOME\Downloads\offer-mails"
```
Only lines that read like offers are kept, with the sender and subject as evidence. Links are
ignored, everything is marked *unverified* (sender addresses can be faked), and emails older than
30 days are skipped unless they state a future expiry (`--max-age-days` changes that).

### Cashback reminders

Record expected cashback when you record a purchase. If it isn't marked paid by its expected date
(90 days after purchase unless you say otherwise), you get a reminder every 14 days to file a
missing-cashback claim. Mark it pending → confirmed → paid on the History page to stop the
reminders. The History page also shows **alert precision**: how many deal alerts led to a
purchase or a working offer within a week.

### Recording what you actually saved

After buying, use **Record a purchase** on the product page: the price without offers, what you
paid at checkout, and what you actually spent if you paid with discounted gift-card balance. Add
expected cashback and its provider. Update the cashback on the History page as it settles. Only
*paid* cashback counts as realised.

## What the labels mean

| Label | Meaning |
|---|---|
| `official` | Seen on amazon.in or the issuer's own site (by you or the extension) |
| `source_listed` / `unverified` | From a feed/file/third party; not independently confirmed |
| `stale` | No source has re-confirmed it recently. Never used for alerts or "confirmed" answers |
| `rules known` / `assumptions` | Whether a scenario relies on anything unconfirmed |
| `synthetic test data` | Demo/fixture data. Never real |
| `seen on amazon.in` | Price captured from a page you viewed |
| `shown on N other product page(s)` | A bank/Amazon Pay offer seen on other products. It may apply here, so it is shown as "possibly", never as confirmed |
