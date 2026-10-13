import os
import requests
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("YOUTUBE_API_KEY")

url = "https://www.googleapis.com/youtube/v3/channels"
params = {
    "part": "snippet,statistics",
    "id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",  # a public channel (Google for Developers)
    "key": key,
}
r = requests.get(url, params=params, timeout=10)
print(r.status_code)
print(r.json())