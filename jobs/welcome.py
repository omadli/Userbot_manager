"""
Welcome-message templates.

After creating a group/channel, the runner sends one of these so the new
chat doesn't sit empty (which is itself a soft-flag with Telegram heuristics).

Templates are intentionally bland — anything richer (links, mentions,
"earn money" copy) lights up anti-spam filters on freshly-created chats.
"""
import random


_TEMPLATES = [
    "Salom!",
    "👋 Salom",
    "Yangi guruh ochildi 🎉",
    "Test xabar — guruh tayyor",
    "Hammaga salom!",
    "🚀",
    "Muloqot boshlandi",
    "Yangi joy yaratildi 🎉",
    "Salom hammaga 👋",
    "Guruh aktiv",
    "🌟",
    "Boshlaymizmi?",
    "Hi 👋",
    "Welcome",
    "Yangi chat — yangi imkoniyatlar 💬",
    "Assalomu alaykum",
    "Tayyor!",
    "🎊",
    "Hello!",
    "Birinchi xabar 📌",
]


def pick_welcome_message() -> str:
    """Return one randomly picked welcome message."""
    return random.SystemRandom().choice(_TEMPLATES)
