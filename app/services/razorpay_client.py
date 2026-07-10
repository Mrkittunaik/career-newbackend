"""
Razorpay order creation for payment/payment.js.

Wire-up checklist:
1. Get RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from https://dashboard.razorpay.com/app/keys
2. Set them in .env
3. (Optional) Set RAZORPAY_WEBHOOK_SECRET to verify webhook signatures for
   `/payments/webhook`, which marks a plan active after payment confirmation.

Until the keys are set, create_order() raises and the payments router
returns a 501 with a message the frontend already knows how to show
(see payment.js `handlePay` catch block).
"""

from app.core.config import settings

PLAN_AMOUNTS_PAISE = {
    "starter": 4900,
    "pro": 39900,
}


def create_order(plan: str):
    if not settings.razorpay_configured:
        raise RuntimeError("Razorpay isn't configured (missing RAZORPAY_KEY_ID/SECRET).")

    if plan not in PLAN_AMOUNTS_PAISE:
        raise ValueError(f"Unknown plan '{plan}'")

    import razorpay

    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    amount = PLAN_AMOUNTS_PAISE[plan]
    order = client.order.create(
        {
            "amount": amount,
            "currency": "INR",
            "notes": {"plan": plan},
        }
    )
    return {
        "orderId": order["id"],
        "amount": amount,
        "currency": "INR",
        "keyId": settings.razorpay_key_id,
    }


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    import razorpay

    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    try:
        client.utility.verify_webhook_signature(body.decode(), signature, settings.razorpay_webhook_secret)
        return True
    except Exception:
        return False
