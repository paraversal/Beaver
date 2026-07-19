from environs import env

env.read_env()

TOKEN = env("BOT_TOKEN")
OWNER_ID = env("OWNER_ID")
SUPERGROUP_ID = env("SUPERGROUP_ID")
SECRET_QUESTION = env("SECRET_QUESTION")
SECRETS = env.list("SECRETS", [])
