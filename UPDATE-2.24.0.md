# Slab Grade 2.24.0 — Cart, watchlist, shipping performance, transaction feedback

This full backend replaces 2.22.0. EasyPost 2.23.0 was abandoned and is not included. Upload backend files only to the backend repository. Database initialization adds new tables without resetting users, cards or transactions.

## Checkout rules
Listings must be at least $9 USD before publication. The combined merchandise subtotal must be at least $19 USD for checkout; shipping never counts toward this minimum. $9 plus $10 qualifies; two $9 cards do not. Cards may come from different sellers. Shipping is $4.99 per card, including when multiple cards come from one seller. The platform fee is 8% of the combined merchandise subtotal (rounded upward to a cent) plus $0.30 once per checkout, proportionally allocated across card orders. Shipping is excluded from the 8% calculation. Stripe processing and Connect fees remain separate.

Cart checkout is one Stripe payment with separate per-card orders, shipping addresses, seller proceeds, returns, tracking and payouts. If any item becomes unavailable before payment settlement, the entire cart payment is refunded and no remaining item is claimed. Retrying payment webhook processing is idempotent. Per-card returns refund only that item's price plus shipping, never the whole mixed-seller payment. Pending checkouts do not reserve cards.

Example: $12.50 + $9.00 = $21.50 merchandise. Shipping $9.98. Buyer total $31.48. Platform commission $2.02. Combined seller proceeds $29.46 before separate Stripe/Connect processing fees.

Cart and watchlist are persisted per account. Unavailable saved listings remain visible for removal; checkout requires available items and revalidates all prices server-side. Maximum 30 distinct cards per checkout.

## Shipping requirements and public profiles
Sellers must submit valid tracking within 3 business days after payment. Days use America/Los_Angeles, excluding weekends and US federal holidays; the deadline is the end of the third business day. First tracking submission earns 2 points within 24 elapsed hours, otherwise 1 by the deadline, otherwise 0. Scores cannot be increased by editing or resubmitting tracking. Public profiles show shipping score, scored sales, on-time submissions and overdue sales awaiting tracking, plus buyer and seller feedback averages.

This is a tracking-submission score. It does not prove carrier acceptance; no carrier-event verification is introduced. Late sellers may still submit tracking to fulfill the order. This release does not automatically cancel unshipped orders or add a monetary late penalty.

Feedback opens only when an order is completed AND the seller payout is marked paid. Both buyer and seller can leave one public 1–5-star review per card transaction. Refunded transactions and failed/pending payouts are ineligible.

## Message deletion
Long-press a message, tap more messages to select them, then Delete messages. Confirmation explains that deletion is private to the selecting user. The other participant retains their copy. Hidden messages do not contribute to that user's unread counter. Server permissions restrict selection to participant-owned conversations.

## Android and deployment
Install Android 0.27.0 with this backend. Cart and watchlist icons use the same purple outline styling as mail and notifications. Full source ZIP omits local.properties and app/google-services.json: preserve your existing local copies. Compile using Android Studio with JDK 17. Set MARKETPLACE_COMMISSION_PERCENT=8 if it remains in Render; checkout rules themselves use fixed 8% and 30 cents. No new API credentials are needed. requirements.txt adds the US holiday calendar package.

Backend validation: 99 tests passed using mocked payment services and local test databases; no live purchases were made. Android source and vector XML checked, but Android compilation was unavailable in this environment (no installed SDK/Gradle distribution).

## Focused beta test
Use Stripe test mode. Save cards to cart/watchlist. Check that $18 in cards cannot pay and $19 can. Buy cards from two sellers, confirm shipping is charged per card, and inspect separate seller orders. Test an unavailable item at payment completion and full refund. Complete a per-card return and confirm the other card remains paid. Submit tracking and inspect profile score. Release seller payout, then leave buyer/seller star reviews. Long-press and multi-delete messages; verify only your view changes.
