"""Connect the agent to a Google Calendar (one-time, on the machine that runs the server).

1. In Google Cloud Console: create a project, enable the Google Calendar API, configure the
   OAuth consent screen (External, add yourself as a test user), then create an OAuth client
   ID of type "Desktop app" and download it to secrets/credentials.json.
2. Run:  .venv/bin/python -m scripts.connect_google_calendar
   A browser opens; sign in with the account whose calendar should take the bookings.
3. secrets/token.json is written. Restart the server: the Admin page shows "Google Calendar connected".

To go back to the built-in calendar, delete secrets/token.json.
"""

import sys

from agent.config import settings
from agent.meetings import SCOPES


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = settings.google_credentials_path
    if not creds_path.exists():
        sys.exit(f"Missing {creds_path}. Download the Desktop-app OAuth client JSON there first (see this file's docstring).")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {settings.google_token_path}.")

    from agent import meetings
    print(meetings.status()["detail"])


if __name__ == "__main__":
    main()
