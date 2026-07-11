from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.db import get_core_db
from app.core.security import get_current_user_id
from app.services import razorpay_client

router = APIRouter(prefix="/payments", tags=["payments"])


class CreateOrderBody(BaseModel):
    plan: str
    method: str | None = None


@router.post("/create-order")
async def create_order(body: CreateOrderBody, user_id: str = Depends(get_current_user_id)):
    try:
        order = razorpay_client.create_order(body.plan)
    except RuntimeError as exc:
        # Matches the message payment.js already displays when this endpoint 501s.
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    db = get_core_db()
    await db.payments.insert_one(
        {
            "user_id": user_id,
            "plan": body.plan,
            "method": body.method,
            "order_id": order["orderId"],
            "amount": order["amount"],
            "currency": order["currency"],
            "status": "created",
            "created_at": datetime.now(timezone.utc),
        }
    )
    return order


@router.post("/webhook", status_code=200)
async def razorpay_webhook(request: Request):
    """
    Point your Razorpay webhook (payment.captured / payment.failed) at this URL
    once RAZORPAY_WEBHOOK_SECRET is set. Marks the matching order paid/failed
    and activates the user's plan.
    """
    from app.services.razorpay_client import verify_webhook_signature

    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = entity.get("order_id")

    db = get_core_db()
    if order_id:
        new_status = "paid" if event == "payment.captured" else "failed"
        await db.payments.update_one({"order_id": order_id}, {"$set": {"status": new_status, "event": event}})
        if new_status == "paid":
            payment = await db.payments.find_one({"order_id": order_id})
            if payment:
                await db.users.update_one(
                    {"_id": ObjectId(payment["user_id"])}, {"$set": {"plan": payment["plan"], "plan_active": True}}
                )

    return {"received": True}
