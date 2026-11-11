"""Create local demo secrets without printing them; refuse to overwrite .env."""

import secrets
from pathlib import Path

template = Path(".env.example").read_text()
for name in ("admin", "payments", "mock-a", "mock-b", "signing", "service-a", "service-b"):
    template = template.replace(f"replace-with-generated-{name}-secret", secrets.token_hex(32))
with Path(".env").open("x") as file:
    file.write(template)
Path(".env").chmod(0o600)
print("Created local .env (mode 0600).")
