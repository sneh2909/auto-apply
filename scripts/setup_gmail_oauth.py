"""One-time Gmail OAuth bootstrap.

Prereqs:
  1. In Google Cloud Console, enable Gmail API on the dedicated sender account's project.
  2. Create OAuth 2.0 Desktop credentials; download as `config/gmail_credentials.json`.
  3. Run this script. It opens a browser, you sign in to the dedicated Gmail account, grant scopes.
  4. The refresh token is saved to `data/gmail_token.json`.

After that, `GmailClient` refreshes the token automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from autoapply.config import get_settings
from autoapply.sources.gmail_alerts import GMAIL_SCOPES


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()

    secrets = settings.gmail_client_secrets
    if not secrets.exists():
        print(f"ERROR: {secrets} not found.", file=sys.stderr)
        print("Download OAuth Desktop credentials from Google Cloud Console first.", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    token_path: Path = settings.gmail_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"OK — token written to {token_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
