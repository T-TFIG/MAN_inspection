"""Secret baked in at build time — DO NOT put a real token here.

The committed value is always empty: when it's empty, every central-backend
feature (heartbeat, central log upload, policy sync) quietly disables itself,
exactly like the email config does when it's missing. CI overwrites this
file's content from a repository secret (``CLIENT_DATA_TOKEN``) right before
running PyInstaller, so only the built .exe ever carries a real value — it
never gets committed to the (public) source repo.

This token must be a GitHub fine-grained PAT scoped to Contents: Read/write
on ONLY the private "T-TFIG/MAN_inspection-data" repo, and nothing else.
"""

CLIENT_DATA_TOKEN = ""
