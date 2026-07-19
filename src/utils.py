from aiogram.types import Message
import coloredlogs
from aiogram import html
import src.env as env

TEXTS = {
    "welcome": lambda name: f"Hi there, {html.bold(name)}! First of all, please reply the following to prove you're not a bot: {html.bold(env.SECRET_QUESTION)}",
    "secret_correct": "That's right! What's your concern?",
    "unknown_person": "whoever you may be"
}

def get_full_name_or_unknown(message: Message) -> str:
    user_name = TEXTS["unknown_person"]
    if message.from_user is not None and message.from_user.full_name is not None:
        user_name = message.from_user.full_name
    return user_name

def install_logs():
    coloredlogs.DEFAULT_FIELD_STYLES["levelname"]["color"] = "white"
    coloredlogs.install(fmt="%(asctime)s %(levelname)s %(module)s %(message)s")

def secret_code_is_correct(attempt: str) -> bool:
    return attempt.strip().lower() in env.SECRETS
