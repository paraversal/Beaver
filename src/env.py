from environs import env

env.read_env()

TOKEN = env("BOT_TOKEN")
ADMIN_IDS = env.list("ADMIN_IDS", subcast=int)
SUPERGROUP_ID = env("SUPERGROUP_ID")
SECRET_QUESTION = env("SECRET_QUESTION")
SECRETS = env.list("SECRETS", [])
