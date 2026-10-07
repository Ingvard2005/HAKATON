"""Run manually on the local demo host, never as an automatic UI action."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from database import DATA_DIR
from services.integrations import GOOGLE_SCOPES

load_dotenv()
if __name__ == "__main__":
    source = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials-google.json"))
    destination = Path(os.getenv("GOOGLE_TOKEN_FILE", str(DATA_DIR / "google-token.json")))
    if not source.is_file():
        raise SystemExit("Скачайте OAuth credentials для Desktop app и укажите GOOGLE_CREDENTIALS_FILE")
    flow = InstalledAppFlow.from_client_secrets_file(str(source), GOOGLE_SCOPES)
    credentials = flow.run_local_server(port=0, open_browser=False, timeout_seconds=180,
        authorization_prompt_message="Откройте ссылку для подключения Google Calendar: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(credentials.to_json(), encoding="utf-8")
    print("Google Calendar подключён. Токен сохранён на сервере.")
