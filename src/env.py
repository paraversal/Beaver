import json

from dotenv import load_dotenv
from os import getenv

load_dotenv()

TOKEN = getenv("BOT_TOKEN")
OWNER_ID = getenv("OWNER_ID")
SUPERGROUP_ID = getenv("SUPERGROUP_ID")
SECRET_QUESTION = getenv("SECRET_QUESTION")
secrets = getenv("SECRETS")
assert TOKEN is not None, "Token not set."
assert OWNER_ID is not None, "Owner ID not set."
assert SUPERGROUP_ID is not None, "Supergroup ID not set."
assert SECRET_QUESTION is not None, "Secret question not set."
assert secrets is not None, "Secrets not set."
SECRETS = json.loads(secrets)
