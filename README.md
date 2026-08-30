# pokemon-arb

Finds ungraded Pokémon cards listed on eBay below their recent-sales value, and
ranks them by how much money you would actually keep.

This is the manual trade — compare a Buy It Now price against PriceCharting's
recent sold prices for the chase cards of a set — run continuously across every
set, with the parts that quietly lose money priced in: sales tax on the way in,
eBay's cut on the way out, and the odds that the listing is not the card the
title claims.

---

## Why "price below comp" is not enough

A naive scanner ranks by `comp − price` and hands you a list of proxies, lots and
Japanese prints. Three things sit between a cheap listing and a profit:

**1. The spread is smaller than it looks.** A $420 Charizard bought at $200 does
not make $220. eBay takes ~13.55% plus $0.40 to sell it, shipping it out costs
~$5, and you paid sales tax on the way in. The board shows profit after all of
that, never the raw gap.

**2. The comp may be the wrong comp.** `Charizard #4` and `Charizard V 154/172`
are both "Charizard". The matcher extracts the card number, the set and the
variant from the listing title and refuses to value a listing it cannot tie to a
specific card — a wrong comp is worse than no comp.

**3. Some listings are not the card.** Proxies, custom metal cards, 12-card lots,
"read description" creases, Japanese prints. Each one gets a named risk flag with
a weight, and the weights discount the score.

So the ranking is **risk-adjusted expected profit**, in dollars:

```
score = profit × match_confidence × (1 − risk_penalty) × liquidity
```

Every term is inspectable on the deal page. A $180 profit on a creased card from
a 2-feedback seller ranks below a $100 profit on a clean one, which is the
ordering you actually want.

---

## Quick start (no API keys needed)

```bash
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
source .venv/bin/activate

pokearb demo-seed        # load a synthetic catalog + build targets
pokearb scan --demo      # scan with generated listings (bargains and traps)
pokearb top              # ranked board in the terminal
pokearb serve            # web UI at http://localhost:8000
```

Demo mode generates listings including the traps a real scan must reject, so you
can see the matching and risk logic work before spending a cent on API access.

---

## Running it for real

You need two credentials.

### eBay Browse API
1. Create a developer account at [developer.ebay.com](https://developer.ebay.com).
2. Make a **production** keyset (Application Keys → App ID / Cert ID).
3. Set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.

The app uses the official Browse API with an application (client-credentials)
token — no scraping. The default keyset allows 5,000 calls/day;
`EBAY_MAX_CALLS_PER_SCAN` (default 400) keeps a single scan well inside that.

### eBay account deletion endpoint (required for production)

eBay will not authenticate a production keyset until it has validated an
endpoint that receives account-deletion notices. Skipping this is why a
correct-looking App ID and Cert ID come back `401 invalid_client`. The app
serves the endpoint at `/ebay/account-deletion`:

```bash
pokearb ebay-token          # generate an acceptable verification token
```

Set both variables, deploy, then check `/diagnostics` — it calls your own
public URL with a random challenge and verifies the reply, which is exactly
what eBay does. Once it passes, paste the same URL and token into the console
under **Application Keys → Notifications → Marketplace Account Deletion** and
hit Save.

```
EBAY_VERIFICATION_TOKEN=<from pokearb ebay-token>
EBAY_DELETION_ENDPOINT_URL=https://your-app.up.railway.app/ebay/account-deletion
```

The URL is hashed into the challenge response, so it must match the console
entry character for character — a trailing slash or `http://` fails validation
with no useful message.

On a real notification the app clears the deleted user's username from any
cached listings and acknowledges with `204`. The listing itself stays: a card
and its price are not personal data. Note that eBay signs these notifications
and this endpoint does not yet verify the signature, so it trusts any caller;
the only effect is clearing a username, and the next scan restores it.

### Comps without a second subscription

`COMP_SOURCE=peer` runs the whole app on eBay alone, with no PriceCharting
token. Be clear-eyed about what changes.

eBay does not sell you sold prices. [Marketplace Insights](https://developer.ebay.com/api-docs/buy/marketplace-insights/static/overview.html),
the sold-items API, is a Limited Release that eBay's own docs describe as
"restricted and not open to new users at this time", and the Browse API returns
active listings only. So the only signal available is what everyone else is
currently **asking**.

Within a single search, listings for the same card and grade are pooled and each
one is valued against the others. Four things keep that honest:

- **A listing never contributes to the comp it is judged against.** Otherwise the
  bargain you are hunting drags down its own reference price, and the cheapest
  listing in a thin group always looks fair.
- **The comp is a low percentile of the asks, not the median** (`PEER_COMP_PERCENTILE`,
  default 0.35). Sellers list optimistically; the ones that clear sit toward the
  bottom of the distribution.
- **Grades are priced separately.** A PSA 10 in the same results must not pull the
  raw comp up, nor a raw card pull the slab comp down.
- **Below `PEER_MIN_SAMPLE` listings a group is not priced at all.** A median of
  three asks is noise, and noise here means buying something.

Lots, proxies, multi-quantity and non-English listings are excluded from the pool
as well as from the results — a 50-card lot at $30 would otherwise drag the
single-card comp down with it.

Every deal produced this way carries a `PEER_COMP` risk flag and is discounted
for it. Read the board as "cheaper than everyone else is asking", not "below
market" — those are different claims, and only the second one is what made your
manual trade work.

Note that this does not reduce the licence exposure. §9(e) covers using eBay
content "either alone or in combination with third-party information" to model
prices, and deriving price statistics purely from eBay data arguably engages
§8.1(d) more, not less.

### PriceCharting API
1. A PriceCharting API subscription gives you a token.
2. Set `PRICECHARTING_TOKEN`.

Then:

```bash
cp .env.example .env      # fill in the two credentials

pokearb sync --price-guide          # download the whole pokemon-cards guide
# or, to start narrow:
pokearb sync -q "charizard" -q "umbreon vmax"
# or, from a CSV you already downloaded:
pokearb sync --csv ~/Downloads/pokemon-cards.csv

pokearb targets --per-set 25        # track the 25 most valuable cards in each set
pokearb scan                        # hit eBay, match, price, rank
pokearb serve
```

`pokearb targets` is the core heuristic, automated: the top-value cards in a set
carry the spread and are the ones sellers most often misprice.

---

## Deploying to Railway

The repo ships a `Dockerfile` and `railway.json`.

**1. Create the service.** Point Railway at this repo; `railway.json` selects the
Dockerfile builder. The image is `python:3.11-slim`, runs as a non-root user, and
starts `pokearb serve`, which binds `0.0.0.0:$PORT` from Railway's injected
`PORT`. The healthcheck is `/healthz`.

A Dockerfile is used rather than Nixpacks deliberately. Nixpacks copies only the
dependency manifest before its `install` phase, so `pip install .` runs before
`src/` exists — and its Nix Python has no `pip` of its own. The Dockerfile has
neither problem, and `docker build -t pokemon-arb . && docker run -p 8000:8000
pokemon-arb` reproduces the deploy locally.

**2. Add Postgres.** SQLite does not survive a redeploy — Railway containers have
ephemeral filesystems, so your scan history and dismissed deals would vanish on
every push. Add the Postgres plugin, then set on the web service:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

`postgres://` and `postgresql://` URLs are rewritten to `postgresql+psycopg://`
automatically, and the pool is configured with `pool_pre_ping` so Railway's idle
connection drops don't surface as errors. Tables are created on boot.

**3. Set the variables** from `.env.example` — at minimum `EBAY_CLIENT_ID`,
`EBAY_CLIENT_SECRET` and `PRICECHARTING_TOKEN`.

**4. Schedule the scans.** Two options:

*Preferred — a separate cron service.* Add a second service from the same repo,
set its start command to `pokearb scan`, and give it a Railway cron schedule
(e.g. `0 */4 * * *`). Point it at the same `DATABASE_URL`. This survives web
restarts and doesn't compete with request handling.

*Simpler — in-process.* Set `SCAN_INTERVAL_MINUTES=240` on the web service and it
runs a scan loop in a background thread. Fine for one instance; don't use it if
you scale the web service past one replica, or every replica will scan.

To refresh comps on a schedule too, use `pokearb sync --price-guide` as a second
cron service (daily is plenty — PriceCharting comps don't move hourly).

**5. Kick the tyres.** `SEED_DEMO_ON_STARTUP=true` seeds the synthetic catalog on
first boot so a fresh deploy isn't an empty page. Turn it off once real comps
are synced.

---

## The web UI

- **`/`** — the ranked board. Filter by status, set, minimum ROI, maximum
  capital, match confidence, or "low risk only". Star a deal to watch it, ✕ to
  dismiss it. A dismissed or bought deal is never resurrected by a later scan.
- **`/deals/{id}`** — the full case for one deal: line-by-line economics from
  comp to profit, why the matcher believes it's that card, the comp ladder
  across grades, and every risk flag with its explanation.
- **`/scans`** — run history, API call usage, and how many listings were rejected
  versus merely under threshold.

  **What to scan for** lives here and is always available, not just when the
  catalog is empty: name cards, set how many top cards per set to track, and
  tick *replace what is tracked* to empty the catalog first so you end up with
  exactly what you asked for. Without replace the new cards are added to what is
  already there and targets are rebuilt across all of it, so a narrower search
  would not actually narrow anything.

  It holds two housekeeping actions, both requiring an explicit confirmation:
  **clear scan history** removes the run records and nothing else, and **start
  from scratch** deletes every deal, listing and run record before optionally
  rescanning. By default neither touches the card catalog, targets or keywords —
  those cost API calls and manual setup to rebuild, and none of them is what
  goes stale. *Also drop the card catalog* on the reset goes all the way.

  **Negative keywords** live here too: any title containing an enabled term is
  dropped before the listing is stored or valued. Proxies, lots, non-English
  prints and graded slabs are already rejected by the matcher without needing a
  keyword — this is for what your own searches keep dragging in: fan art,
  acrylic stands, keychains, oversized promos. Each term shows how many
  listings it has excluded, so a term stuck at zero is probably misspelled.
- **`/diagnostics`** — when eBay or PriceCharting rejects you, this says which
  check failed and why. Credentials appear only as fingerprints (length plus a
  few characters), enough to compare against the eBay console without exposing
  the key. `pokearb doctor` is the same thing on the command line.
- **Cancelling** — a running scan or sync can be stopped from the banner. It is
  cooperative, not a kill: an in-flight eBay request finishes and its listings
  are kept, and a cancelled sync keeps the cards it already committed, so a
  partial catalog is still usable. Checkpoints are between targets, between
  listings within a target, and on every row of a price-guide download, so a
  cancel lands in about the time of one HTTP request. The banner switches to
  "Cancelling…" as soon as you click, since the stop happens at the next
  checkpoint rather than instantly. Runs left behind by a killed process are
  closed out as `interrupted` on the next boot rather than showing as running
  forever.
- **`/api/deals`** — JSON, for anything you want to build on top.
  Swagger at `/api/docs`.

---

## How a scan works

```
sync comps  →  build targets  →  search eBay  →  match  →  price  →  rank
```

For each target the scanner computes the **highest landed price that could still
clear your profit and ROI bars**, and uses it as the eBay `price` filter ceiling,
sorted cheapest-first. Fairly-priced listings are never fetched, so almost the
entire API budget goes to plausible bargains.

Every listing is then parsed for card number, set, variant, language, grading
company and hazard words; matched against the target plus any other catalogued
card sharing a number in the title; and valued against the comp that fits how
it's actually being sold — a PSA 10 slab is priced against the PSA 10 comp, not
the raw one.

### What gets rejected outright

Card number mismatch · Japanese/Korean/German prints against English comps ·
proxy / custom / replica / metal wording · lots, bundles, repacks and
pick-your-card · multi-quantity listings.

### What gets flagged and discounted

Damage wording · "read description" / "as is" · below-98% or near-zero feedback
sellers · no returns · international shipping · thin or stale comps · discounts
so large they imply the listing is fake · weak match confidence.

---

## CLI

| Command | What it does |
|---|---|
| `pokearb init` | Create the database schema |
| `pokearb demo-seed` | Load the synthetic catalog and build targets |
| `pokearb sync --price-guide` | Download the full PriceCharting category |
| `pokearb sync --csv PATH` | Load an already-downloaded price guide CSV |
| `pokearb sync -q "charizard"` | Look up specific cards by name |
| `pokearb targets --per-set 25` | Track the top-N most valuable cards per set |
| `pokearb scan [--demo]` | Search, match, price and rank |
| `pokearb top` | Print the ranked board |
| `pokearb stats` | Catalog counts and last scan |
| `pokearb doctor` | Diagnose credential and connectivity problems |
| `pokearb ebay-token` | Generate an eBay verification token |
| `pokearb serve` | Run the web app |

---

## Configuration

All settings are environment variables (see `.env.example`). The ones worth
tuning:

| Variable | Default | Meaning |
|---|---|---|
| `SELL_FEE_RATE` | `0.1355` | eBay final value fee incl. payment processing |
| `OUTBOUND_SHIPPING_CENTS` | `500` | What it costs you to ship a sold card |
| `BUYER_TAX_RATE` | `0.08` | Sales tax you pay when buying |
| `CONDITION_HAIRCUT` | `0.90` | Raw cards clear below the running average |
| `MIN_PROFIT_CENTS` | `1000` | Floor for a deal to be recorded |
| `MIN_ROI` | `0.25` | ROI floor |
| `MIN_MATCH_CONFIDENCE` | `0.62` | How sure the matcher must be |
| `MIN_SALES_VOLUME` | `3` | Below this, comps are treated as noise |
| `TOO_GOOD_TO_BE_TRUE_DISCOUNT` | `0.90` | Discounts past this are flagged |
| `SCAN_TOP_CARDS_PER_SET` | `25` | Targets built per set |
| `EBAY_MAX_CALLS_PER_SCAN` | `400` | API budget per scan |
| `LISTING_FRESHNESS_MINUTES` | `360` | Max age of displayed eBay data; clamped to 360 |

`CONDITION_HAIRCUT` is the one to revisit first. It encodes "an ungraded card of
unknown condition sells for less than the average of all recent ungraded sales."
If you buy conservatively and the cards arrive better than modeled, raise it.

---

## Listing data goes stale after six hours

eBay's API License Agreement §8.1(c) says displayed listing information may not
be more than six hours behind eBay. The board enforces that **per request**, not
as a flag written during a scan — if scanning stops for any reason (rate limit,
expired credentials, a dead scheduler), cached listings age out on their own
instead of sitting on the board looking current.

Past the window, deals drop off the board and the API, and the deal page returns
`409` with a refresh prompt rather than rendering a stale price, title or seller.
`LISTING_FRESHNESS_MINUTES` can tighten the window but is clamped to 360 — no
configuration can put the app out of compliance.

The practical consequence is that **scan cadence and the display window are
coupled**: scanning every 4 hours keeps the board populated, every 8 hours means
it is empty much of the time. `pokearb scan` on a Railway cron schedule of
`0 */4 * * *` is the intended setup, and the in-process scheduler logs a warning
if `SCAN_INTERVAL_MINUTES` exceeds the window.

Note this is separate from the 3-day horizon in `scan.py`, which is how long
before a listing is presumed gone from eBay entirely.

---

## Layout

```
src/pokemon_arb/
  config.py            settings, Postgres URL normalization
  models.py            Product, Listing, Deal, Target, ScanRun, PricePoint
  store.py             upserts and read queries
  money.py             integer-cent arithmetic
  freshness.py         the six-hour display rule, in one place
  diagnostics.py       credential and connectivity checks
  ebay_notifications.py  account-deletion challenge and purge
  matching/
    normalize.py       eBay title -> card number, variant, language, hazards
    matcher.py         listing <-> product confidence scoring
  pipeline/
    scoring.py         comp selection, fees, tax, risk flags, ranking
    peer_comps.py      valuing a listing against other live eBay listings
    scan.py            the orchestrator
  sources/
    ebay.py            Browse API client (OAuth, filters, pagination)
    pricecharting.py   JSON API + CSV price guide
    demo.py            offline fixtures, including the traps
  web/                 FastAPI app, templates, CSS
  cli.py               pokearb commands
tests/                 101 tests
```

Run them with `pytest`; lint with `ruff check src tests`.

---

## Limitations worth knowing

- **Comps are a model, not a promise.** PriceCharting's ungraded price is an
  average of recent sales across conditions. A specific card can sell for well
  under it. The condition haircut is a blunt instrument for this.
- **Photos are not examined.** Every judgement comes from the title and
  structured listing fields. The most common real-world loss — a card that
  photographs worse than it's described — is exactly what this cannot catch, so
  the risk flags are a filter for what to open, not a substitute for looking.
- **Only Buy It Now.** Auctions are filtered out by default; their displayed
  price is not the price you pay.
- **English cards only.** Non-English listings are rejected rather than valued,
  because they trade against different comps.
- **Both APIs have terms, and eBay's are restrictive.** Beyond the six-hour rule
  above, the [API License Agreement](https://developer.ebay.com/join/api-license-agreement)
  §9(e) bars using eBay content "either alone or in combination with third-party
  information, to suggest or model prices for items listed on eBay Site", §9(c)
  bars building anything competitive with eBay services, and §8.1(d) requires
  written permission to derive average selling price or aggregated seller data.
  Read those before pointing this at production keys and decide for yourself
  where your use sits.
