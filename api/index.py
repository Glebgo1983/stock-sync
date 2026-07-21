from fastapi import FastAPI, Request, Response
import httpx
import multipart
import os
import re
import secrets
from api.functions import new_order_handler, update_orders
from api.mpfit_webhook import handle_stock_event, webhook_secret
from api.stock_sync import run_stock_sync
from api.kiz_sync import run_kiz_sync
from api.sync_journal import get_summary, get_recent_entries
#from api.handlers import set_time, update, clear_keys, hash_password
from urllib.parse import unquote, urlparse

app = FastAPI()
cron_secret = os.getenv("CRON_SECRET")


@app.get('/api/update')
async def get_handler():
    try:
        result = await update_orders()
        return result 
    except Exception as e:
        print(e)
        return e

@app.post('/api/create')
async def post_handler(request: Request):
    try:
        body = await request.body()
        print(body)
        body = await request.json()
        print(body)
        result = await new_order_handler(body)
        return result
    except Exception as e:
        print(e)
        return e
        
@app.post('/api/webhook')
async def webhook(request: Request):
    ...

@app.get('/api/sync-stock')
async def sync_stock_handler(request: Request):
    if not cron_secret or request.headers.get("authorization") != f"Bearer {cron_secret}":
        return Response(status_code=401)
    dry_run = request.query_params.get("dry_run", "false").lower() == "true"
    try:
        result = await run_stock_sync(dry_run)
        return result
    except Exception as e:
        print(e)
        return Response(status_code=500, content=str(e))

@app.get('/api/sync-kiz')
async def sync_kiz_handler(request: Request):
    if not cron_secret or request.headers.get("authorization") != f"Bearer {cron_secret}":
        return Response(status_code=401)
    dry_run = request.query_params.get("dry_run", "false").lower() == "true"
    try:
        result = await run_kiz_sync(dry_run)
        return result
    except Exception as e:
        print(e)
        return Response(status_code=500, content=str(e))

@app.post('/api/stock-webhook')
async def stock_webhook_handler(request: Request):
    auth_header = request.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        auth_header = auth_header[len("Bearer "):]
    provided = request.query_params.get("secret") or auth_header
    if not webhook_secret or not secrets.compare_digest(provided, webhook_secret):
        return Response(status_code=401)
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            result = await handle_stock_event(client, body)
        return result
    except Exception as e:
        print(e)
        return Response(status_code=500, content=str(e))

@app.get('/api/sync-log')
async def sync_log_handler(request: Request):
    if not cron_secret or request.headers.get("authorization") != f"Bearer {cron_secret}":
        return Response(status_code=401)
    limit = int(request.query_params.get("limit", "100"))
    try:
        summary = get_summary()
        entries = get_recent_entries(limit)
        return {**summary, "entries": entries}
    except Exception as e:
        print(e)
        return Response(status_code=500, content=str(e))

