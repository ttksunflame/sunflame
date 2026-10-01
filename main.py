from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import base64
from typing import Optional
import secrets
from pydantic import BaseModel
from datetime import datetime
import os
from dotenv import load_dotenv
import json
import hmac
import hashlib
from supabase import create_client, Client
from postgrest.exceptions import APIError
import razorpay
import random
import re
import string

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

# Admin login
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()


def require_admin(request: Request):
    # No WWW-Authenticate header on the 401, so the browser never pops its own login box.
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not set on the server")
    auth = request.headers.get("Authorization", "")
    try:
        user, _, pw = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
    except Exception:
        raise HTTPException(401, "Wrong username or password")
    if not (secrets.compare_digest(user, ADMIN_USERNAME) and secrets.compare_digest(pw, ADMIN_PASSWORD)):
        raise HTTPException(401, "Wrong username or password")


@app.get("/")
async def website():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(HERE, "admin.html"))


app.mount("/images", StaticFiles(directory=os.path.join(HERE, "images"), check_dir=False), name="images")
app.mount("/videos", StaticFiles(directory=os.path.join(HERE, "videos"), check_dir=False), name="videos")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== MODELS =====================

class CartItem(BaseModel):
    product_id: int
    quantity: int = 1

class Cart(BaseModel):
    items: list[CartItem]

class CheckoutRequest(BaseModel):
    customer_name: str
    customer_email: str
    customer_phone: str
    shipping_address: str
    shipping_city: str
    shipping_pincode: str
    items: list[CartItem]

class PaymentVerificationRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

# ===================== PRODUCTS =====================

@app.get("/api/products")
async def get_products(category_id: int = None):
    """Fetch all products, optionally filtered by category"""
    query = supabase.table("products").select("*").eq("is_active", True)
    
    if category_id:
        query = query.eq("category_id", category_id)
    
    products = query.order("id", desc=True).execute().data
    ids = [p["id"] for p in products]
    first_image = {}
    if ids:
        images = supabase.table("product_images").select("product_id, image_url, sort_order") \
            .in_("product_id", ids).order("sort_order").execute().data
        for img in images:
            first_image.setdefault(img["product_id"], img["image_url"])
    for p in products:
        p["image_url"] = first_image.get(p["id"]) or p.get("image_url")
    return {"products": products}

@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    """Fetch a single product with images"""
    product = supabase.table("products").select("*").eq("id", product_id).single().execute()
    images = supabase.table("product_images").select("*").eq("product_id", product_id).order("sort_order").execute()
    
    return {
        "product": product.data,
        "images": images.data
    }

@app.get("/api/categories")
async def get_categories():
    """Fetch all categories"""
    response = supabase.table("categories").select("*").order("sort_order").execute()
    return {"categories": response.data}

# ===================== CART =====================

@app.post("/api/cart/validate")
async def validate_cart(cart: Cart):
    """Validate cart items: check stock, prices, etc."""
    total = 0
    gst = 0
    items_detail = []
    
    for item in cart.items:
        product = supabase.table("products").select("*").eq("id", item.product_id).single().execute()
        prod = product.data
        
        if not prod:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        
        if prod["stock"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {prod['name']}")
        
        line_total = prod["price"] * item.quantity
        total += line_total
        
        items_detail.append({
            "product_id": item.product_id,
            "name": prod["name"],
            "price": prod["price"],
            "quantity": item.quantity,
            "line_total": line_total
        })
    
    # Calculate GST (18% standard)
    gst = round(total * 0.18, 2)
    grand_total = total + gst
    
    return {
        "subtotal": total,
        "gst": gst,
        "shipping": 0,
        "total": grand_total,
        "items": items_detail
    }

# ===================== ORDERS =====================

@app.post("/api/orders/create")
async def create_order(checkout: CheckoutRequest):
    """Create order and return Razorpay order ID"""
    
    # Validate cart
    cart_validation = await validate_cart(Cart(items=checkout.items))
    
    total_paise = int(cart_validation["total"] * 100)
    
    # Generate order number
    order_number = f"ORD-{datetime.now().strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"
    
    # Create order in Supabase
    order_data = {
        "order_number": order_number,
        "customer_name": checkout.customer_name,
        "customer_email": checkout.customer_email,
        "customer_phone": checkout.customer_phone,
        "shipping_address": checkout.shipping_address,
        "shipping_city": checkout.shipping_city,
        "shipping_pincode": checkout.shipping_pincode,
        "subtotal": cart_validation["subtotal"],
        "gst": cart_validation["gst"],
        "shipping_cost": 0,
        "total": cart_validation["total"],
        "status": "pending"
    }
    
    order_response = supabase.table("orders").insert(order_data).execute()
    order_id = order_response.data[0]["id"]
    
    # Insert order items
    for item in checkout.items:
        product = supabase.table("products").select("*").eq("id", item.product_id).single().execute()
        prod = product.data
        
        item_data = {
            "order_id": order_id,
            "product_id": item.product_id,
            "product_name": prod["name"],
            "quantity": item.quantity,
            "unit_price": prod["price"],
            "total_price": prod["price"] * item.quantity
        }
        supabase.table("order_items").insert(item_data).execute()
    
    # Create Razorpay order
    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    
    razorpay_order = client.order.create({
        "amount": total_paise,
        "currency": "INR",
        "receipt": order_number,
        "notes": {
            "order_id": order_id,
            "customer_name": checkout.customer_name
        }
    })
    
    # Update order with Razorpay order ID
    supabase.table("orders").update({"razorpay_order_id": razorpay_order["id"]}).eq("id", order_id).execute()
    
    return {
        "order_id": order_id,
        "order_number": order_number,
        "razorpay_order_id": razorpay_order["id"],
        "amount": cart_validation["total"],
        "amount_paise": total_paise,
        "key_id": RAZORPAY_KEY_ID
    }

# ===================== PAYMENT VERIFICATION =====================

@app.post("/api/payments/verify")
async def verify_payment(verification: PaymentVerificationRequest):
    """Verify Razorpay payment signature"""
    
    # Verify signature
    signature = verification.razorpay_signature
    body = verification.razorpay_order_id + "|" + verification.razorpay_payment_id
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256
    ).hexdigest()
    
    if signature != expected_signature:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Get order and update payment status
    order = supabase.table("orders").select("*").eq("razorpay_order_id", verification.razorpay_order_id).single().execute()
    order_data = order.data
    order_id = order_data["id"]
    
    # Update order status to paid
    supabase.table("orders").update({
        "status": "paid",
        "razorpay_payment_id": verification.razorpay_payment_id,
        "razorpay_signature": signature
    }).eq("id", order_id).execute()
    
    # Log payment
    payment_data = {
        "order_id": order_id,
        "razorpay_payment_id": verification.razorpay_payment_id,
        "razorpay_order_id": verification.razorpay_order_id,
        "amount": order_data["total"],
        "currency": "INR",
        "status": "captured"
    }
    supabase.table("payments").insert(payment_data).execute()
    
    return {
        "success": True,
        "order_id": order_id,
        "order_number": order_data["order_number"],
        "amount": order_data["total"],
        "payment_id": verification.razorpay_payment_id
    }

@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    """Fetch order details"""
    order = supabase.table("orders").select("*").eq("id", order_id).single().execute()
    items = supabase.table("order_items").select("*").eq("order_id", order_id).execute()
    
    return {
        "order": order.data,
        "items": items.data
    }

# ===================== ADMIN =====================

@app.get("/api/admin/check", dependencies=[Depends(require_admin)])
async def admin_check():
    return {"ok": True}


@app.get("/api/admin/orders", dependencies=[Depends(require_admin)])
async def get_all_orders(status: str = None):
    """Fetch all orders, optionally filtered by status"""
    query = supabase.table("orders").select("*").order("created_at", desc=True)
    
    if status:
        query = query.eq("status", status)
    
    response = query.execute()
    return {"orders": response.data}

@app.patch("/api/admin/orders/{order_id}/status", dependencies=[Depends(require_admin)])
async def update_order_status(order_id: int, status: str):
    """Update order status"""
    valid_statuses = ["pending", "paid", "confirmed", "shipped", "delivered", "cancelled"]
    
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid_statuses}")
    
    response = supabase.table("orders").update({"status": status}).eq("id", order_id).execute()
    return {"order": response.data[0]}

ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def unique_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "product"
    slug, n = base, 2
    while supabase.table("products").select("id").eq("slug", slug).execute().data:
        slug, n = f"{base}-{n}", n + 1
    return slug


@app.post("/api/admin/products", dependencies=[Depends(require_admin)])
async def create_product(
    name: str = Form(...),
    price: float = Form(...),
    mrp: str = Form(""),
    stock: int = Form(100),
    category_id: Optional[int] = Form(None),
    description: str = Form(""),
    images: list[UploadFile] = File(default=[]),
):
    """Create a product and upload its images to Supabase Storage"""
    if category_id is None:
        cats = supabase.table("categories").select("id").order("sort_order").limit(1).execute().data
        if not cats:
            raise HTTPException(400, "No categories exist yet")
        category_id = cats[0]["id"]

    uploads = []
    for img in images:
        ext = ALLOWED_IMAGE_TYPES.get(img.content_type)
        if ext is None:
            raise HTTPException(400, f"{img.filename}: unsupported type {img.content_type}")
        data = await img.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{img.filename} is larger than 5 MB")
        uploads.append((ext, data, img.content_type))

    try:
        rows = supabase.table("products").insert({
            "name": name,
            "slug": unique_slug(name),
            "description": description,
            "price": price,
            "mrp": float(mrp) if mrp.strip() else price,
            "stock": stock,
            "category_id": category_id,
            "is_active": True,
        }).execute().data
    except APIError as e:
        raise HTTPException(400, f"Could not create product: {e.message}")
    if not rows:
        raise HTTPException(500, "Product insert returned no row")
    product = rows[0]

    # The product row is already committed, so clean it up rather than leave a
    # half-made product that a retry would duplicate.
    image_urls = []
    try:
        for i, (ext, data, content_type) in enumerate(uploads):
            path = f"{product['id']}/{i}.{ext}"
            supabase.storage.from_("product-images").upload(
                path, data, {"content-type": content_type}
            )
            url = supabase.storage.from_("product-images").get_public_url(path)
            supabase.table("product_images").insert(
                {"product_id": product["id"], "image_url": url, "sort_order": i}
            ).execute()
            image_urls.append(url)
        if image_urls:
            supabase.table("products").update({"image_url": image_urls[0]}).eq("id", product["id"]).execute()
    except Exception as e:
        supabase.table("products").delete().eq("id", product["id"]).execute()
        raise HTTPException(400, f"Image upload failed, product not created: {e}")

    return {"product": product, "images": image_urls}


@app.delete("/api/admin/products/{product_id}", dependencies=[Depends(require_admin)])
async def remove_product(product_id: int):
    # Hidden rather than deleted: past orders still reference the product.
    rows = supabase.table("products").update({"is_active": False}).eq("id", product_id).execute().data
    if not rows:
        raise HTTPException(404, "Product not found")
    return {"removed": product_id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
