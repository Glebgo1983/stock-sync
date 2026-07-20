from fastapi import FastAPI, Request, Response
import multipart
import os
import re
from api.functions import new_order_handler, update_orders
from api.stock_sync import run_stock_sync
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

