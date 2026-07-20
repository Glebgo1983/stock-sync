from dotenv import load_dotenv
import time
import os



load_dotenv(dotenv_path=".env.local")
pochta_token = os.getenv("POCHTA_APP_AUTH_TOKEN")
pochta_key = os.getenv("POCHTA_CLIENT_AUTH_KEY")
pochta_headers = {
  "X-User-Authorization": f"Basic {pochta_key}",
  "Authorization": f"AccessToken {pochta_token}",
  "Content-Type": "application/json;charset=UTF-8"
}
                  
async def get_pochta_tracking_number(client, order):
  url = f"https://otpravka-api.pochta.ru/1.0/backlog/search?query={order}"
  result = await client.get(url=url, headers=pochta_headers)
  result = result.json()
  return result
