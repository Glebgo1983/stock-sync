from dotenv import load_dotenv
import time
import os

load_dotenv(dotenv_path=".env.local")
cdek_client = os.getenv("CDEK_CLIENT")
cdek_secret = os.getenv("CDEK_SECRET")


async def obtain_cdek_token(client, redis_client):
  url = "https://api.cdek.ru/v2/oauth/token?grant_type=client_credentials"
  result = await client.post(url=url, auth=(cdek_client, cdek_secret))
  result = result.json()
  timestamp = time.now()
  token_data = {"token": result["access_token"], "timestamp": timestamp}
  redis_client.hset("insales_mpfit_cdek_token", mapping=token_data)
  return result["access_token"]

async def get_cdek_token(client, redis_client):
  token_data = r.hgetall("insales_mpfit_cdek_token")
  timestamp = time.now()
  if timestamp - token_data["timestamp"] >= 3600:
    token = await obtain_cdek_token(client, redis_client)
    return token
  else:
    return token_data["token"]
    
async def get_cdek_tracking_number(client, redis_client, id):
  token = get_cdek_token(client, redis_client)
  url = f"https://api.cdek.ru/v2/orders?im_number={id}"
  headers = {"Authorization": f"Bearer {token}"}
  result = await client.get(url=url, headers=headers)
  result = result.json()
  return result
