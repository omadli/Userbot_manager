"""
Starter script templates shipped with the app.

Each entry is a self-contained `async def main(client, account, params)`.
The runner connects the Telethon client + decrypts the session, then calls
this function once per selected account. Return value is logged (≤500 chars);
raise to mark *that account* as failed (siblings continue).

When you add a new starter:
  1. Define the code as a triple-quoted constant.
  2. Append to STARTER_TEMPLATES with a stable `key`.

The script editor in the UI offers these as a "dan boshlash" dropdown.
"""


# ---------------------------------------------------------------------------
# 1. Akkaunt ma'lumotlari (eng oddiy — sanity check)
# ---------------------------------------------------------------------------
GET_ME = '''\
async def main(client, account, params):
    """Akkaunt haqida Telegram bilgan ma'lumotlarni qaytaradi."""
    me = await client.get_me()
    return {
        "id": me.id,
        "username": me.username,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "phone": me.phone,
        "premium": bool(getattr(me, "premium", False)),
        "verified": bool(getattr(me, "verified", False)),
    }
'''


# ---------------------------------------------------------------------------
# 2. Hamma chatlardan chiqish (filter bilan)
# ---------------------------------------------------------------------------
LEAVE_ALL_CHATS = '''\
async def main(client, account, params):
    """
    Akkaunt a'zo bo'lgan barcha guruh/kanallardan chiqish.

    params:
      keep_titles (list[str])  — bu so'zlardan birini o'z ichiga olgan chatlar saqlanadi
      max_leaves  (int)        — maksimal nechta chatni tark etishni cheklash (default 0 = barcha)
      delay_sec   (float)      — chiqishlar orasidagi pauza (default 1.5)
    """
    import asyncio
    from telethon.tl.functions.channels import LeaveChannelRequest

    keep = [s.lower() for s in (params.get("keep_titles") or [])]
    max_leaves = int(params.get("max_leaves") or 0)
    delay = float(params.get("delay_sec") or 1.5)

    left, kept = [], []
    async for dialog in client.iter_dialogs():
        if not (dialog.is_channel or dialog.is_group):
            continue
        title = (dialog.title or "").lower()

        if any(k in title for k in keep):
            kept.append(dialog.title)
            continue

        try:
            await client(LeaveChannelRequest(dialog.entity))
            left.append(dialog.title)
            await asyncio.sleep(delay)
        except Exception as e:
            kept.append(f"{dialog.title} (xato: {type(e).__name__})")

        if max_leaves and len(left) >= max_leaves:
            break

    return {"left": len(left), "kept": len(kept), "examples": left[:5]}
'''


# ---------------------------------------------------------------------------
# 3. Profilni yangilash (random ism / bio / username)
# ---------------------------------------------------------------------------
UPDATE_PROFILE = '''\
async def main(client, account, params):
    """
    Akkaunt profilini yangilaydi.

    params:
      first_name  (str)  — yangi ism (bo'sh — tegmaydi)
      last_name   (str)  — familiya
      bio         (str)  — about
      username    (str)  — @username (band bo'lsa xato)
    """
    from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest

    fields = {}
    if params.get("first_name"): fields["first_name"] = params["first_name"]
    if params.get("last_name"):  fields["last_name"]  = params["last_name"]
    if params.get("bio"):        fields["about"]      = params["bio"]

    if fields:
        await client(UpdateProfileRequest(**fields))

    if params.get("username"):
        try:
            await client(UpdateUsernameRequest(username=params["username"]))
        except Exception as e:
            return {"profile_updated": bool(fields), "username_error": str(e)}

    me = await client.get_me()
    return {
        "profile_updated": bool(fields),
        "username": me.username,
        "first_name": me.first_name,
    }
'''


# ---------------------------------------------------------------------------
# 4. Botga xabar yuborish (mass DM yo'q — bu o'zining botiga)
# ---------------------------------------------------------------------------
SEND_MESSAGE_TO_BOT = '''\
async def main(client, account, params):
    """
    Berilgan bot/foydalanuvchiga xabar yuborish. Asosan o'z botingizga
    "online check" yoki referal tasdiqlash uchun.

    params:
      target  (str)  — @username yoki t.me/...
      text    (str)  — yuboriladigan matn
    """
    target = params.get("target")
    text = params.get("text", "Salom!")
    if not target:
        raise ValueError("`target` parametri kerak")

    entity = await client.get_entity(target)
    msg = await client.send_message(entity, text)
    return {"sent_to": target, "message_id": msg.id}
'''


# ---------------------------------------------------------------------------
# 5. Kanal tarixini parsing (so'nggi xabarlardan ma'lumot olish)
# ---------------------------------------------------------------------------
SCRAPE_CHANNEL = '''\
async def main(client, account, params):
    """
    Kanal/guruh tarixidan so'nggi xabarlarni o'qib, qisqa statistika qaytaradi.

    params:
      target  (str)  — @channelname yoki t.me/...
      limit   (int)  — nechta xabar (default 100, max 500)
    """
    target = params.get("target")
    limit = min(500, int(params.get("limit") or 100))
    if not target:
        raise ValueError("`target` parametri kerak")

    entity = await client.get_entity(target)
    msgs = await client.get_messages(entity, limit=limit)

    text_msgs = [m for m in msgs if m.text]
    media_msgs = [m for m in msgs if m.media]
    total_views = sum(m.views or 0 for m in msgs)

    return {
        "target": target,
        "fetched": len(msgs),
        "text_count": len(text_msgs),
        "media_count": len(media_msgs),
        "total_views": total_views,
        "avg_views": round(total_views / len(msgs), 1) if msgs else 0,
        "first_id": msgs[-1].id if msgs else None,
        "last_id": msgs[0].id if msgs else None,
    }
'''


# ---------------------------------------------------------------------------
# 6. Kontakt qo'shish (telefon raqamlar bilan)
# ---------------------------------------------------------------------------
ADD_CONTACTS = '''\
async def main(client, account, params):
    """
    Bir nechta telefon raqamni kontakt sifatida qo'shadi.

    params:
      contacts  (list[dict])  — [{"phone": "+998...", "first_name": "...", "last_name": "..."}]
    """
    from telethon.tl.functions.contacts import ImportContactsRequest
    from telethon.tl.types import InputPhoneContact
    import random

    raw = params.get("contacts") or []
    if not raw:
        raise ValueError("`contacts` parametri bo'sh")

    contacts = [
        InputPhoneContact(
            client_id=random.randint(1, 10**9),
            phone=c["phone"],
            first_name=c.get("first_name", ""),
            last_name=c.get("last_name", ""),
        )
        for c in raw
    ]
    result = await client(ImportContactsRequest(contacts=contacts))
    return {
        "imported": len(result.imported),
        "users_found": len(result.users),
        "retry_contacts": len(result.retry_contacts),
    }
'''


# ---------------------------------------------------------------------------
# 7. Spam check yangidan
# ---------------------------------------------------------------------------
RECHECK_SPAM = '''\
async def main(client, account, params):
    """
    @SpamBot orqali spam holatini qaytadan tekshiradi va Account.is_spam ni yangilaydi.

    Diqqat: yangi sessiyalar uchun bu sessiyani o'chirib yuborishi mumkin —
    akkauntning yoshini 1 soatdan oshganini tekshirib qo'ying.
    """
    import asyncio
    from asgiref.sync import sync_to_async
    from accounts.models import Account

    bot = await client.get_entity("@spambot")
    await client.send_message(bot, "/start")
    await asyncio.sleep(3)
    msgs = await client.get_messages(bot, limit=1)
    text = (msgs[0].text or "") if msgs else ""

    # Spam belgilari
    keywords = ["Unfortunately", "Afsuski", "К сожалению", "restricting"]
    is_spam = any(k in text for k in keywords)

    @sync_to_async
    def _save():
        Account.objects.filter(pk=account.pk).update(is_spam=is_spam)

    await _save()
    return {"is_spam": is_spam, "reply_preview": text[:200]}
'''


# ---------------------------------------------------------------------------
# Catalog — show in the editor's "dan boshlash" dropdown
# ---------------------------------------------------------------------------
STARTER_TEMPLATES = [
    {
        'key': 'get_me',
        'name': 'Akkaunt ma\'lumotlari',
        'description': 'Eng oddiy: akkauntning Telegram tomonidan saqlangan ma\'lumotlarini qaytaradi. Sanity-check uchun ideal.',
        'code': GET_ME,
    },
    {
        'key': 'leave_all',
        'name': 'Hamma chatlardan chiqish',
        'description': 'A\'zo bo\'lgan barcha guruh/kanallardan chiqadi. `keep_titles` bilan ba\'zilarini saqlash mumkin.',
        'code': LEAVE_ALL_CHATS,
    },
    {
        'key': 'update_profile',
        'name': 'Profilni yangilash',
        'description': 'Ism / familiya / bio / username\'ni yangilaydi. Random nomlar bilan ko\'p akkauntni "humanize" qilish.',
        'code': UPDATE_PROFILE,
    },
    {
        'key': 'send_message',
        'name': 'Botga xabar yuborish',
        'description': 'Berilgan @username ga xabar yuboradi. Referal tasdiqlash yoki o\'z botingizga ping uchun.',
        'code': SEND_MESSAGE_TO_BOT,
    },
    {
        'key': 'scrape',
        'name': 'Kanaldan xabarlarni o\'qish',
        'description': 'Kanal/guruhning so\'nggi xabarlaridan statistika oladi (count, view, media). Read-only.',
        'code': SCRAPE_CHANNEL,
    },
    {
        'key': 'add_contacts',
        'name': 'Kontaktlar qo\'shish',
        'description': 'Telefon raqamlar ro\'yxatidan kontakt qo\'shadi. Telegram registratsiyasini tekshirish ham bilvosita ishlaydi.',
        'code': ADD_CONTACTS,
    },
    {
        'key': 'recheck_spam',
        'name': 'Qaytadan spam tekshirish',
        'description': '@SpamBot bilan spam statusini tekshiradi va DB\'da yangilaydi. Yangi sessiyalarda xavfli — eski akkauntlarda ishlating.',
        'code': RECHECK_SPAM,
    },
]
