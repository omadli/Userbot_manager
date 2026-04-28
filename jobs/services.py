"""
Telethon automation helpers used by job runners.

Each function here targets a single Telegram account. It returns a dict
describing the outcome so the caller (runner) can log per-account events
without having to catch Telethon-specific exceptions itself.

Exceptions that are meaningful for the runner loop (FloodWaitError) are
re-raised; all others are mapped to a structured failure result.
"""
import re
from asgiref.sync import sync_to_async

from telethon.tl.functions.channels import (
    CreateChannelRequest,
    JoinChannelRequest,
    GetParticipantRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.messages import DeleteChatUserRequest
from telethon.tl.functions.messages import (
    ExportChatInviteRequest,
    ImportChatInviteRequest,
    GetMessagesViewsRequest,
    SendReactionRequest,
    SendVoteRequest,
    StartBotRequest,
)
from telethon.tl.types import (
    PeerChannel, ReactionEmoji, Channel, Chat,
    ChannelParticipantCreator, ChannelParticipantAdmin,
)
from telethon.errors import (
    FloodWaitError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    SessionExpiredError,
    PhoneNumberBannedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    ChatTitleEmptyError,
    UnauthorizedError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelsTooMuchError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    MessageIdsEmptyError,
    ReactionInvalidError,
    MessageNotModifiedError,
    BotResponseTimeoutError,
)

from accounts.services import get_client, get_client_for_account
from accounts.models import Account


# Exceptions that mean the session is dead — no point retrying this account.
SESSION_DEAD_EXCEPTIONS = (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    SessionExpiredError,
    UnauthorizedError,
)

# Exceptions that mean the account is banned — mark is_spam + is_active=False.
ACCOUNT_BANNED_EXCEPTIONS = (
    PhoneNumberBannedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)


async def _mark_session_dead(account_pk):
    await Account.objects.filter(pk=account_pk).aupdate(is_active=False)


async def _mark_account_banned(account_pk):
    await Account.objects.filter(pk=account_pk).aupdate(is_active=False, is_spam=True)


# ---------------------------------------------------------------------------
# Target parsing (username or invite link)
# ---------------------------------------------------------------------------

_TME_URL_RE = re.compile(r'^(?:https?://)?t\.me/(.+)$', re.IGNORECASE)
_USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{3,30}$')


def parse_target(raw):
    """
    Classify a user-supplied chat reference.

    Accepts:
      @channelname              → ('username', 'channelname')
      channelname               → ('username', 'channelname')
      https://t.me/channelname  → ('username', 'channelname')
      https://t.me/+HASH        → ('invite',   'HASH')
      https://t.me/joinchat/HASH→ ('invite',   'HASH')

    Returns (kind, payload, original). kind is 'username', 'invite',
    or 'unknown' when nothing matched — the caller logs the latter.
    """
    if raw is None:
        return ('unknown', '', '')
    s = raw.strip()
    if not s:
        return ('unknown', '', '')

    original = s

    if s.startswith('@'):
        s = s[1:]
        if _USERNAME_RE.match(s):
            return ('username', s, original)
        return ('unknown', s, original)

    m = _TME_URL_RE.match(s)
    if m:
        path = m.group(1).strip('/')
        if path.startswith('joinchat/'):
            return ('invite', path[len('joinchat/'):], original)
        if path.startswith('+'):
            return ('invite', path[1:], original)
        # Strip any trailing /post_id
        name = path.split('/', 1)[0]
        if _USERNAME_RE.match(name):
            return ('username', name, original)
        return ('unknown', name, original)

    if _USERNAME_RE.match(s):
        return ('username', s, original)

    return ('unknown', s, original)


# ---------------------------------------------------------------------------
# Message URL parsing (for views/reactions/votes)
# ---------------------------------------------------------------------------

_MSG_URL_RE = re.compile(
    r'^(?:https?://)?t\.me/(?P<prefix>c/)?(?P<name>[^/]+)/(?P<msg_id>\d+)(?:\?.*)?$',
    re.IGNORECASE,
)


def parse_message_url(url):
    """
    Parse a Telegram message link.

    Returns (kind, peer_ref, msg_id, original):
      kind='public'    → peer_ref is the channel username (no @)
      kind='private_c' → peer_ref is int (internal channel ID, no -100 prefix)
      kind='unknown'   → caller should log and skip
    """
    if not url:
        return ('unknown', '', 0, url or '')
    m = _MSG_URL_RE.match(url.strip())
    if not m:
        return ('unknown', '', 0, url)
    msg_id = int(m.group('msg_id'))
    name = m.group('name')
    if m.group('prefix'):
        try:
            internal = int(name)
        except ValueError:
            return ('unknown', '', 0, url)
        return ('private_c', internal, msg_id, url)
    if _USERNAME_RE.match(name):
        return ('public', name, msg_id, url)
    return ('unknown', '', 0, url)


async def _resolve_message_peer(client, kind, peer_ref):
    """Turn (kind, peer_ref) from parse_message_url into a Telethon input peer."""
    if kind == 'public':
        return await client.get_entity(peer_ref)
    if kind == 'private_c':
        # The -100 prefix is how Bot API exposes channels; for MTProto use PeerChannel.
        return await client.get_entity(PeerChannel(peer_ref))
    raise ValueError(f"Unknown peer kind: {kind}")


async def create_group_for_account(account, title, megagroup=True, welcome_message=None):
    """
    Creates a Telegram supergroup (megagroup=True) or broadcast channel
    (megagroup=False — acts as channel; use create_channel_for_account for clarity).

    When `welcome_message` is a non-empty string, it is sent to the new chat
    immediately after creation (same client session — no second connect).
    A failure on send is logged silently; the create itself still counts.

    Returns:
        { 'success': bool,
          'telegram_id': int | None,
          'invite_link': str | None,
          'welcome_sent': bool,
          'error': str,
          'error_type': str,
          'stop_account': bool }  # True → don't retry this account

    FloodWaitError is re-raised so the runner can sleep the exact delay.
    """
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {
            'success': False,
            'error': f"Ulanib bo'lmadi: {e}",
            'error_type': type(e).__name__,
            'stop_account': False,
        }

    try:
        # Verify session is alive before burning a CreateChannel quota.
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {
                'success': False,
                'error': "Sessiya yaroqsiz",
                'error_type': 'NoMe',
                'stop_account': True,
            }

        title = (title or '').strip()[:128]  # Telegram title limit
        if not title:
            return {
                'success': False,
                'error': "Guruh nomi bo'sh",
                'error_type': 'EmptyTitle',
                'stop_account': False,
            }

        result = await client(CreateChannelRequest(
            title=title,
            about='',
            megagroup=megagroup,
            broadcast=not megagroup,
        ))
        chat = result.chats[0]

        invite_link = None
        try:
            invite = await client(ExportChatInviteRequest(chat))
            invite_link = getattr(invite, 'link', None)
        except FloodWaitError:
            # Don't fail the whole op for the invite — just skip it.
            pass
        except Exception:
            pass

        welcome_sent = False
        if welcome_message:
            try:
                await client.send_message(chat, welcome_message[:4096])
                welcome_sent = True
            except FloodWaitError:
                # Don't bubble up — the chat is already created. Soft-skip.
                pass
            except Exception:
                pass

        return {
            'success': True,
            'telegram_id': int(chat.id),
            'invite_link': invite_link,
            'welcome_sent': welcome_sent,
            'error': '',
            'error_type': '',
            'stop_account': False,
        }

    except FloodWaitError:
        # Let the runner handle the wait.
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {
            'success': False,
            'error': "Sessiya chiqarib yuborilgan — qayta kirish kerak",
            'error_type': type(e).__name__,
            'stop_account': True,
        }
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {
            'success': False,
            'error': "Akkaunt bloklangan",
            'error_type': type(e).__name__,
            'stop_account': True,
        }
    except ChatTitleEmptyError as e:
        return {
            'success': False,
            'error': "Guruh nomi bo'sh deb qabul qilindi",
            'error_type': type(e).__name__,
            'stop_account': False,
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__,
            'stop_account': False,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def join_chat_for_account(account, target):
    """
    Join a single chat from an account.

    `target` is the raw user input (@username, t.me/name, t.me/+hash, etc.).
    Returns the same shape as create_group_for_account.

    Extra distinct outcomes:
      - `already_member` boolean in the result dict when the account is
        already in the chat (counted as success by the runner)
      - telegram_id + invite_link populated on success when available
    """
    kind, payload, original = parse_target(target)
    if kind == 'unknown':
        return {
            'success': False,
            'error': f"Noto'g'ri format: {original!r}",
            'error_type': 'InvalidTarget',
            'stop_account': False,
        }

    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {
            'success': False,
            'error': f"Ulanib bo'lmadi: {e}",
            'error_type': type(e).__name__,
            'stop_account': False,
        }

    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {
                'success': False, 'error': "Sessiya yaroqsiz",
                'error_type': 'NoMe', 'stop_account': True,
            }

        chat = None
        already = False

        if kind == 'username':
            try:
                entity = await client.get_entity(payload)
            except (UsernameNotOccupiedError, UsernameInvalidError) as e:
                return {
                    'success': False,
                    'error': f"@{payload}: foydalanuvchi/kanal topilmadi",
                    'error_type': type(e).__name__,
                    'stop_account': False,
                }
            except ValueError as e:
                return {
                    'success': False,
                    'error': f"@{payload}: {e}",
                    'error_type': 'ValueError', 'stop_account': False,
                }
            try:
                result = await client(JoinChannelRequest(entity))
                chat = result.chats[0] if result.chats else entity
            except UserAlreadyParticipantError:
                chat = entity
                already = True

        else:  # invite
            try:
                result = await client(ImportChatInviteRequest(payload))
                chat = result.chats[0] if result.chats else None
            except UserAlreadyParticipantError:
                # Telethon surfaces this via the invite endpoint; we can't
                # resolve the chat without the hash re-lookup.
                chat = None
                already = True

        telegram_id = int(chat.id) if chat is not None else None
        return {
            'success': True,
            'already_member': already,
            'telegram_id': telegram_id,
            'chat_title': getattr(chat, 'title', None) if chat is not None else None,
            'invite_link': None,  # joining doesn't give us an invite link
            'error': '',
            'error_type': '',
            'stop_account': False,
            'target': original,
        }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {
            'success': False,
            'error': "Sessiya chiqarib yuborilgan — qayta kirish kerak",
            'error_type': type(e).__name__, 'stop_account': True,
        }
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {
            'success': False, 'error': "Akkaunt bloklangan",
            'error_type': type(e).__name__, 'stop_account': True,
        }
    except ChannelsTooMuchError as e:
        # Account hit the ~500 chat limit. Can't join more from this account.
        return {
            'success': False,
            'error': "Akkaunt chat limitiga yetgan (ChannelsTooMuch)",
            'error_type': type(e).__name__,
            'stop_account': True,
        }
    except (InviteHashExpiredError, InviteHashInvalidError) as e:
        return {
            'success': False,
            'error': f"Invite link yaroqsiz yoki muddati tugagan: {original}",
            'error_type': type(e).__name__, 'stop_account': False,
        }
    except ChannelPrivateError as e:
        return {
            'success': False,
            'error': f"Kanal maxfiy yoki akkaunt banlangan: {original}",
            'error_type': type(e).__name__, 'stop_account': False,
        }
    except Exception as e:
        return {
            'success': False, 'error': str(e),
            'error_type': type(e).__name__, 'stop_account': False,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def react_to_message_for_account(account, message_url, emojis):
    """
    Send a reaction (single emoji picked from `emojis`) to one message.

    `emojis` is a list[str]; a random one is picked to add variety across
    accounts. An empty list clears the reaction.
    """
    import random as _rnd

    kind, peer_ref, msg_id, original = parse_message_url(message_url)
    if kind == 'unknown':
        return {
            'success': False,
            'error': f"Xabar URL yaroqsiz: {original!r}",
            'error_type': 'InvalidURL', 'stop_account': False,
        }

    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {
            'success': False, 'error': f"Ulanib bo'lmadi: {e}",
            'error_type': type(e).__name__, 'stop_account': False,
        }

    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {'success': False, 'error': "Sessiya yaroqsiz",
                    'error_type': 'NoMe', 'stop_account': True}

        try:
            peer = await _resolve_message_peer(client, kind, peer_ref)
        except ChannelPrivateError:
            return {'success': False, 'error': "Kanal maxfiy / akkaunt a'zo emas",
                    'error_type': 'ChannelPrivateError', 'stop_account': False}
        except (UsernameNotOccupiedError, UsernameInvalidError):
            return {'success': False, 'error': "Kanal topilmadi",
                    'error_type': 'UsernameNotOccupied', 'stop_account': False}

        emoji = _rnd.choice(emojis) if emojis else None
        reaction_arg = [ReactionEmoji(emoticon=emoji)] if emoji else []

        try:
            await client(SendReactionRequest(
                peer=peer, msg_id=msg_id, reaction=reaction_arg,
            ))
            return {
                'success': True,
                'emoji': emoji, 'message_url': original,
                'error': '', 'error_type': '', 'stop_account': False,
            }
        except FloodWaitError:
            raise
        except ReactionInvalidError as e:
            return {
                'success': False,
                'error': f"Reaksiya qabul qilinmadi (emoji={emoji!r})",
                'error_type': type(e).__name__, 'stop_account': False,
            }
        except MessageNotModifiedError:
            # Already reacted with this emoji — treat as success.
            return {
                'success': True, 'emoji': emoji, 'already_reacted': True,
                'message_url': original,
                'error': '', 'error_type': '', 'stop_account': False,
            }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {'success': False, 'error': "Akkaunt bloklangan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def vote_poll_for_account(account, poll_url, strategy='random', option_index=0):
    """
    Vote in a Telegram poll message.

    `strategy`:
      'random' — pick a uniformly-random option
      'fixed'  — pick `option_index` (0-based)

    Returns the usual result dict, plus:
      chosen_index — int (which option was voted for)
      chosen_text  — str
    """
    import random as _rnd

    kind, peer_ref, msg_id, original = parse_message_url(poll_url)
    if kind == 'unknown':
        return {
            'success': False,
            'error': f"So'rovnoma URL yaroqsiz: {original!r}",
            'error_type': 'InvalidURL', 'stop_account': False,
        }

    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': f"Ulanib bo'lmadi: {e}",
                'error_type': type(e).__name__, 'stop_account': False}

    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {'success': False, 'error': "Sessiya yaroqsiz",
                    'error_type': 'NoMe', 'stop_account': True}

        try:
            peer = await _resolve_message_peer(client, kind, peer_ref)
        except ChannelPrivateError:
            return {'success': False, 'error': "Kanal maxfiy",
                    'error_type': 'ChannelPrivateError', 'stop_account': False}
        except (UsernameNotOccupiedError, UsernameInvalidError):
            return {'success': False, 'error': "Kanal topilmadi",
                    'error_type': 'UsernameNotOccupied', 'stop_account': False}

        msg = await client.get_messages(peer, ids=msg_id)
        if msg is None:
            return {'success': False, 'error': "Xabar topilmadi",
                    'error_type': 'MessageNotFound', 'stop_account': False}

        poll = getattr(getattr(msg, 'media', None), 'poll', None)
        if poll is None:
            return {'success': False, 'error': "Xabar so'rovnoma emas",
                    'error_type': 'NotAPoll', 'stop_account': False}

        answers = list(poll.answers or [])
        if not answers:
            return {'success': False, 'error': "So'rovnomada javoblar yo'q",
                    'error_type': 'NoAnswers', 'stop_account': False}

        if strategy == 'fixed':
            idx = max(0, min(len(answers) - 1, int(option_index)))
        else:
            idx = _rnd.randrange(len(answers))

        chosen = answers[idx]
        chosen_text = ''
        # TextWithEntities in recent Telethon versions has .text
        if hasattr(chosen, 'text'):
            chosen_text = getattr(chosen.text, 'text', None) or str(chosen.text)

        try:
            await client(SendVoteRequest(
                peer=peer, msg_id=msg_id, options=[chosen.option],
            ))
            return {
                'success': True,
                'chosen_index': idx,
                'chosen_text': chosen_text,
                'error': '', 'error_type': '', 'stop_account': False,
            }
        except FloodWaitError:
            raise
        except MessageNotModifiedError:
            # Already voted with this option.
            return {
                'success': True, 'chosen_index': idx, 'chosen_text': chosen_text,
                'already_voted': True,
                'error': '', 'error_type': '', 'stop_account': False,
            }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {'success': False, 'error': "Akkaunt bloklangan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def press_start_for_account(account, bot_username, start_param=''):
    """
    Press /start on a bot, optionally with a `start_param` (referral code).

    Uses StartBotRequest when start_param is provided (this is the
    canonical way to invoke a deep-link referral). When start_param is
    empty, falls back to sending "/start" as a plain message so the bot
    at least sees the onboarding intent.
    """
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': f"Ulanib bo'lmadi: {e}",
                'error_type': type(e).__name__, 'stop_account': False}

    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {'success': False, 'error': "Sessiya yaroqsiz",
                    'error_type': 'NoMe', 'stop_account': True}

        bot_username = (bot_username or '').lstrip('@').strip()
        if not bot_username:
            return {'success': False, 'error': "Bot username bo'sh",
                    'error_type': 'EmptyUsername', 'stop_account': False}

        try:
            bot = await client.get_entity(bot_username)
        except (UsernameNotOccupiedError, UsernameInvalidError) as e:
            return {'success': False, 'error': f"Bot @{bot_username} topilmadi",
                    'error_type': type(e).__name__, 'stop_account': False}

        try:
            if start_param:
                await client(StartBotRequest(
                    bot=bot, peer=bot, start_param=str(start_param),
                ))
            else:
                # No referral code — /start as a plain message reproduces the
                # UI "Start" button behaviour without needing a parameter.
                await client.send_message(bot, '/start')
            return {
                'success': True, 'bot': bot_username, 'start_param': start_param,
                'error': '', 'error_type': '', 'stop_account': False,
            }
        except FloodWaitError:
            raise
        except BotResponseTimeoutError:
            # Bot didn't reply — but /start itself was delivered. Treat as success.
            return {
                'success': True, 'bot': bot_username, 'start_param': start_param,
                'bot_silent': True,
                'error': '', 'error_type': '', 'stop_account': False,
            }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {'success': False, 'error': "Akkaunt bloklangan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def boost_views_for_account(account, message_urls):
    """
    Increment view counters on the given message URLs from a single account.

    `message_urls` is a list[str]. Messages are grouped by channel so we
    issue one GetMessagesViewsRequest per peer per account (batching is
    faster and looks less suspicious than 1 request per message).

    Returns the normal result dict plus:
      viewed_count    — number of messages the server accepted
      failed_targets  — list of (url, reason) for messages we couldn't view
    """
    # Group by (kind, peer_ref) preserving msg order for logging.
    grouped = {}
    unknowns = []
    for url in message_urls:
        kind, peer_ref, msg_id, original = parse_message_url(url)
        if kind == 'unknown':
            unknowns.append(original)
            continue
        grouped.setdefault((kind, peer_ref), []).append((msg_id, original))

    if not grouped and not unknowns:
        return {
            'success': False,
            'error': "Hech qaysi xabar URL tanilmadi",
            'error_type': 'NoTargets', 'stop_account': False,
        }

    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {
            'success': False, 'error': f"Ulanib bo'lmadi: {e}",
            'error_type': type(e).__name__, 'stop_account': False,
        }

    viewed = 0
    failed_targets = []

    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {
                'success': False, 'error': "Sessiya yaroqsiz",
                'error_type': 'NoMe', 'stop_account': True,
            }

        # Mark invalid URLs up front.
        for u in unknowns:
            failed_targets.append((u, "URL formati yaroqsiz"))

        for (kind, peer_ref), items in grouped.items():
            msg_ids = [m[0] for m in items]
            try:
                peer = await _resolve_message_peer(client, kind, peer_ref)
            except FloodWaitError:
                raise
            except ChannelPrivateError:
                for _, u in items:
                    failed_targets.append((u, "Kanal maxfiy yoki akkaunt a'zo emas"))
                continue
            except (UsernameNotOccupiedError, UsernameInvalidError):
                for _, u in items:
                    failed_targets.append((u, "Kanal topilmadi"))
                continue
            except ValueError as e:
                for _, u in items:
                    failed_targets.append((u, f"Peer resolve xato: {e}"))
                continue

            try:
                await client(GetMessagesViewsRequest(
                    peer=peer, id=msg_ids, increment=True,
                ))
                viewed += len(msg_ids)
            except FloodWaitError:
                raise
            except MessageIdsEmptyError:
                for _, u in items:
                    failed_targets.append((u, "Xabarlar topilmadi"))
            except Exception as e:
                for _, u in items:
                    failed_targets.append((u, f"{type(e).__name__}: {e}"))

        return {
            'success': viewed > 0 or not message_urls,
            'viewed_count': viewed,
            'failed_targets': failed_targets,
            'error': '' if viewed else "Hech bir xabar ko'rilmadi",
            'error_type': '' if viewed else 'AllFailed',
            'stop_account': False,
        }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {
            'success': False,
            'error': "Sessiya chiqarib yuborilgan",
            'error_type': type(e).__name__, 'stop_account': True,
        }
    except ACCOUNT_BANNED_EXCEPTIONS as e:
        await _mark_account_banned(account.pk)
        return {
            'success': False, 'error': "Akkaunt bloklangan",
            'error_type': type(e).__name__, 'stop_account': True,
        }
    except Exception as e:
        return {
            'success': False, 'error': str(e),
            'error_type': type(e).__name__, 'stop_account': False,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Leave non-admin chats — bulk cleanup of groups/channels where the user
# is neither the creator nor an admin.
# ---------------------------------------------------------------------------

import asyncio
import random


async def leave_non_admin_chats_for_account(
    account, *,
    kind='group',
    delay_min=2.0,
    delay_max=6.0,
    max_chats=None,
):
    """
    Iterate this account's dialogs and leave every chat of `kind` where
    the user is neither creator nor admin.

    `kind`:
      - 'group'    → megagroups (modern groups) + legacy basic Chats
      - 'channel'  → broadcast channels only

    Returns a list of dicts:
        [{'chat_id': int, 'title': str,
          'action': 'left' | 'kept_admin' | 'error',
          'reason': str, 'error_type': str (only when action='error')}]

    Detection of admin status uses the entity's own `creator` /
    `admin_rights` attributes that come back with iter_dialogs() — no
    extra GetParticipant call per chat, which would multiply API load
    by N and trigger FloodWait on busy accounts.
    """
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return [{
            'chat_id': 0, 'title': '',
            'action': 'error',
            'reason': f"Ulanib bo'lmadi: {e}",
            'error_type': type(e).__name__,
        }]

    results = []
    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return [{
                'chat_id': 0, 'title': '',
                'action': 'error',
                'reason': "Sessiya yaroqsiz",
                'error_type': 'NoMe',
            }]
        my_id = me.id

        count = 0
        async for dialog in client.iter_dialogs():
            entity = dialog.entity

            # Filter by kind
            if isinstance(entity, Channel):
                is_megagroup = bool(getattr(entity, 'megagroup', False))
                if kind == 'group' and not is_megagroup:
                    continue
                if kind == 'channel' and is_megagroup:
                    continue
            elif isinstance(entity, Chat):
                # Legacy basic group — only relevant for kind='group'
                if kind != 'group':
                    continue
            else:
                # User / SecretChat / etc. — skip
                continue

            count += 1
            if max_chats and count > max_chats:
                break

            title = getattr(entity, 'title', '') or '<noname>'

            # Determine admin status without extra API calls
            is_creator = bool(getattr(entity, 'creator', False))
            has_admin_rights = bool(getattr(entity, 'admin_rights', None))

            if is_creator:
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'kept_admin', 'reason': 'creator',
                })
                continue
            if has_admin_rights:
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'kept_admin', 'reason': 'admin',
                })
                continue

            # Basic Chat doesn't expose `creator` reliably — fall back to
            # checking participants. This is rare; cost is bounded by the
            # number of basic groups, which is usually tiny.
            if isinstance(entity, Chat):
                try:
                    full = await client.get_permissions(entity, my_id)
                    if full.is_creator:
                        results.append({
                            'chat_id': entity.id, 'title': title,
                            'action': 'kept_admin', 'reason': 'creator (basic)',
                        })
                        continue
                    if full.is_admin:
                        results.append({
                            'chat_id': entity.id, 'title': title,
                            'action': 'kept_admin', 'reason': 'admin (basic)',
                        })
                        continue
                except Exception:
                    pass  # fall through to leave

            # Not admin — leave
            try:
                await client.delete_dialog(entity)
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'left', 'reason': '',
                })
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                # Sleep + one retry; if it floods again, mark error and move on
                await asyncio.sleep(min(wait + 1, 60))
                try:
                    await client.delete_dialog(entity)
                    results.append({
                        'chat_id': entity.id, 'title': title,
                        'action': 'left', 'reason': f'after FloodWait {wait}s',
                    })
                except Exception as e2:
                    results.append({
                        'chat_id': entity.id, 'title': title,
                        'action': 'error',
                        'reason': str(e2)[:200],
                        'error_type': type(e2).__name__,
                    })
            except SESSION_DEAD_EXCEPTIONS as e:
                await _mark_session_dead(account.pk)
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'error',
                    'reason': "Sessiya chiqarib yuborilgan",
                    'error_type': type(e).__name__,
                })
                break  # stop entirely — session is dead
            except ACCOUNT_BANNED_EXCEPTIONS as e:
                await _mark_account_banned(account.pk)
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'error',
                    'reason': "Akkaunt bloklangan",
                    'error_type': type(e).__name__,
                })
                break
            except Exception as e:
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'error',
                    'reason': str(e)[:200],
                    'error_type': type(e).__name__,
                })

            # Pause between leaves to avoid floodwait
            await asyncio.sleep(random.uniform(delay_min, delay_max))

        return results

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
