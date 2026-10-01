# Linkcart

Real product comparisons for Cambodia, with a manual procurement queue and test payments.

## Run locally

Use Python 3.10+ and install `requirements.txt` (`Pillow` validates and normalizes uploads).
Copy `.env.example` to `.env`, choose a strong `ADMIN_TOKEN`, and run `python3 server.py`.
Open http://localhost:5173. SQLite orders are stored in `data/linkcart.sqlite3`; preserve and back up this directory. Credentials stay server-side.

## Product search

The home page starts empty. There is no sample catalog or generated comparison data. Paste a supported HTTPS retailer URL or enter a product/model name. Accessible Amazon storefronts are searched in US, Japan, UK, and Germany. Optional `SERPAPI_KEY` enables broader Google Shopping comparisons. Optional `RAINFOREST_API_KEY` supports Amazon product extraction. Retailers may block direct reads; failures and missing prices are visible and do not fall back to invented offers. USD conversions use Frankfurter reference exchange rates. Regions describe storefront/search markets, not verified warehouse origins. Similarity does not guarantee the same model, variant, or warranty.

## Product-image uploads (currently disabled)

Upload JPEG, PNG, or WebP up to 5 MB and 20 megapixels. The browser previews the selected photo. The server validates actual image content, strips metadata, resizes it, and sends a normalized JPEG (under 500 KB) to SerpApi's Image API, then requests Google Lens product matches using `image_id`. No public hosting is required, and Linkcart does not persist uploaded photos. SerpApi processes the photo under its own retention/privacy terms. Do not upload personal or sensitive images.

Image search is disabled at the user’s request: controls are hidden and the API rejects uploads. To restore it later, re-enable the controls and endpoint, then set `SERPAPI_KEY` in `.env`. Without a key, the UI reports the missing configuration and link/name searches remain available. The provider integration has fixture-based tests, but a live image request has not been verified because credentials were not supplied. See https://serpapi.com/google-lens-upload-an-image and https://serpapi.com/google-lens-products-api.

## Payments and testing

`PAYMENT_MODE=mock` is the default requested test mode. Products and their prices are real search results, while payment is simulated. Choose ABA Pay / KHQR or Credit / debit card, review the total, and submit checkout. Test payment supports success, decline/retry, and cancellation. No card/bank details are collected and no valid payable QR is generated. Success creates a clearly labeled test order that staff can advance through purchased, shipped, and delivered. Mock fees default to 8% plus $12 shipping, and are illustrative only.

Mock and real orders are separated in customer and staff views. Existing test orders remain saved. Never manually buy a product based on a test order. Customer access is tied to the browser's HttpOnly cookie; this version does not provide account recovery.

For actual ABA sandbox testing, set `PAYMENT_MODE=sandbox`, `PAYWAY_ENV=sandbox`, sandbox merchant credentials, the business's explicit `SERVICE_FEE_RATE` and `SHIPPING_USD`, and `ENABLE_LIVE_CHECKOUT=true`. Set a PayWay-approved HTTPS `BASE_URL` and arrange domain/IP whitelisting with ABA. For real payments later, use matching `PAYMENT_MODE=production` and `PAYWAY_ENV=production` with production credentials. Sandbox/production checkout requires a parsed USD price and explicitly in-stock product, approved pricing, and enabled provider configuration.

The PayWay adapter signs hosted forms for `abapay_khqr` and `cards`; bank/card details are entered only on PayWay. The server verifies callback HMAC, independently checks the transaction's approved status, USD amount, transaction ID, and refund amount, and handles repeated callbacks idempotently. The customer can resume pending checkout and check payment status. Checkout totals are calculated server-side from session-scoped offers and cannot be overwritten by client input. Actual PayWay sandbox/production transactions have not been tested without merchant credentials. Official integration reference: https://developer.payway.com.kh/purchase-14530820e0.

## Validation

`python3 -m unittest discover -s tests -v`

25 tests cover extraction, model relevance, real currencies, provider errors, upload validation and metadata removal, visual match mapping, order durability and idempotency, access isolation, fulfillment transitions, and payment callback verification. A real Amazon link and mock card decline/success flow were also checked in the browser.

## Before public launch

This is a local test platform, not a deployed production service. Public launch requires TLS hosting with a production application server, individual staff/customer authentication and recovery, encrypted backups and retention rules, distributed rate limits and outbound egress controls, provider sandbox approval, scheduled payment reconciliation, customer notifications, merchant-approved refund/privacy/service policies, accurate delivery/customs pricing, and final variant/stock/price verification. Python `http.server` is for local testing. Live visual search depends on SerpApi credentials, and reliable retailer coverage requires approved product-data providers.
