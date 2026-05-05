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
    PeerChannel, ReactionEmoji, ReactionCustomEmoji, Channel, Chat, User,
    ChannelParticipantCreator, ChannelParticipantAdmin,
    MessageMediaPhoto, MessageMediaDocument, MessageMediaPoll,
    MessageMediaWebPage, MessageMediaContact, MessageMediaGeo,
    KeyboardButtonCallback, KeyboardButtonUrl, KeyboardButtonSwitchInline,
    KeyboardButtonGame, KeyboardButtonBuy, KeyboardButtonUrlAuth,
    KeyboardButtonRequestPhone, KeyboardButtonRequestGeoLocation,
    DocumentAttributeVideo, DocumentAttributeAudio, DocumentAttributeAnimated,
    DocumentAttributeSticker,
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
try:
    from telethon.errors import InviteRequestSentError
except ImportError:
    class InviteRequestSentError(Exception):
        pass

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
        request_sent = False

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
            except InviteRequestSentError:
                chat = entity
                request_sent = True

        else:  # invite
            try:
                result = await client(ImportChatInviteRequest(payload))
                chat = result.chats[0] if result.chats else None
            except UserAlreadyParticipantError:
                chat = None
                already = True
            except InviteRequestSentError:
                chat = None
                request_sent = True

        telegram_id = int(chat.id) if chat is not None else None
        return {
            'success': True,
            'already_member': already,
            'request_sent': request_sent,
            'telegram_id': telegram_id,
            'chat_title': getattr(chat, 'title', None) if chat is not None else None,
            'invite_link': None,
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

            # Reliable admin/creator detection: query the participant record
            # via get_permissions. The earlier shortcut of reading
            # `entity.creator` / `entity.admin_rights` from iter_dialogs() was
            # missing some non-admin chats — Telegram doesn't always populate
            # those fields on dialog snapshots, so a False entity.creator
            # could be a real "not creator" or simply "field absent". An
            # extra API call per chat avoids that ambiguity.
            try:
                perms = await client.get_permissions(entity, my_id)
                if getattr(perms, 'is_creator', False):
                    results.append({
                        'chat_id': entity.id, 'title': title,
                        'action': 'kept_admin', 'reason': 'creator',
                    })
                    continue
                if getattr(perms, 'is_admin', False):
                    results.append({
                        'chat_id': entity.id, 'title': title,
                        'action': 'kept_admin', 'reason': 'admin',
                    })
                    continue
            except FloodWaitError:
                raise
            except Exception as e:
                # If we can't determine admin status, conservatively SKIP
                # leaving (safer to keep a chat than to leave an admin one
                # by accident). Mark as error so the user sees what happened.
                results.append({
                    'chat_id': entity.id, 'title': title,
                    'action': 'error',
                    'reason': f"admin status aniqlanmadi: {e}",
                    'error_type': type(e).__name__,
                })
                continue

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


# ---------------------------------------------------------------------------
# Leave a specific list of chats by id (used by the live-chats UI where the
# user has already manually picked which chats to leave).
# ---------------------------------------------------------------------------

async def leave_specific_chats_for_account(
    account, chat_ids, *, delay_min=2.0, delay_max=6.0,
):
    """Leave each chat in `chat_ids` (resolved from the account's dialogs).

    Unlike `leave_non_admin_chats_for_account`, NO admin filter is applied
    — the caller already chose. Returns the same per-chat results format.
    """
    target_ids = set(int(x) for x in chat_ids)

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

        async for dialog in client.iter_dialogs():
            if not target_ids:
                break
            entity = dialog.entity
            entity_id = getattr(entity, 'id', None)
            if entity_id is None or entity_id not in target_ids:
                continue
            target_ids.discard(entity_id)
            title = getattr(entity, 'title', '') or '<noname>'

            try:
                await client.delete_dialog(entity)
                results.append({
                    'chat_id': entity_id, 'title': title,
                    'action': 'left', 'reason': '',
                })
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                await asyncio.sleep(min(wait + 1, 60))
                try:
                    await client.delete_dialog(entity)
                    results.append({
                        'chat_id': entity_id, 'title': title,
                        'action': 'left', 'reason': f'after FloodWait {wait}s',
                    })
                except Exception as e2:
                    results.append({
                        'chat_id': entity_id, 'title': title,
                        'action': 'error',
                        'reason': str(e2)[:200],
                        'error_type': type(e2).__name__,
                    })
            except SESSION_DEAD_EXCEPTIONS as e:
                await _mark_session_dead(account.pk)
                results.append({
                    'chat_id': entity_id, 'title': title,
                    'action': 'error',
                    'reason': "Sessiya chiqarib yuborilgan",
                    'error_type': type(e).__name__,
                })
                break
            except Exception as e:
                results.append({
                    'chat_id': entity_id, 'title': title,
                    'action': 'error',
                    'reason': str(e)[:200],
                    'error_type': type(e).__name__,
                })

            await asyncio.sleep(random.uniform(delay_min, delay_max))

        # Any IDs we never met as dialogs — note them
        for missed_id in target_ids:
            results.append({
                'chat_id': missed_id, 'title': f'<id {missed_id}>',
                'action': 'error',
                'reason': "Akkaunt dialoglarida topilmadi (allaqachon chiqib ketgan?)",
                'error_type': 'NotFound',
            })

        return results

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# List live Telegram dialogs (groups + channels) for the account-detail UI.
# ---------------------------------------------------------------------------

async def list_dialogs_for_account(account, *, max_dialogs=500):
    """Pull dialogs and split into groups, channels, DMs (contacts +
    non-contacts) and bots.

    Returns:
        {
          'success': bool,
          'error': str (if not success),
          'groups':   [...],
          'channels': [...],
          'dms':      [...],   # private chats with users (non-bot)
          'bots':     [...],   # private chats with bots
        }
    """
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': f"Ulanib bo'lmadi: {e}",
                'groups': [], 'channels': [], 'dms': [], 'bots': []}
    try:
        me = await client.get_me()
        if not me:
            return {'success': False, 'error': "Sessiya yaroqsiz",
                    'groups': [], 'channels': [], 'dms': [], 'bots': []}

        groups, channels, dms, bots = [], [], [], []
        n = 0
        async for dialog in client.iter_dialogs():
            n += 1
            if n > max_dialogs:
                break
            entity = dialog.entity
            if isinstance(entity, Channel):
                is_megagroup = bool(getattr(entity, 'megagroup', False))
                is_creator = bool(getattr(entity, 'creator', False))
                is_admin = bool(getattr(entity, 'admin_rights', None))
                row = {
                    'id': entity.id,
                    'title': getattr(entity, 'title', '') or '<noname>',
                    'username': getattr(entity, 'username', None),
                    'is_creator': is_creator,
                    'is_admin': is_admin,
                    'unread': getattr(dialog, 'unread_count', 0),
                    'members': getattr(entity, 'participants_count', None),
                }
                if is_megagroup:
                    groups.append(row)
                else:
                    channels.append(row)
            elif isinstance(entity, Chat):
                groups.append({
                    'id': entity.id,
                    'title': getattr(entity, 'title', '') or '<noname>',
                    'username': None,
                    'is_creator': False,
                    'is_admin': False,
                    'unread': getattr(dialog, 'unread_count', 0),
                    'members': getattr(entity, 'participants_count', None),
                })
            elif isinstance(entity, User):
                first = getattr(entity, 'first_name', '') or ''
                last = getattr(entity, 'last_name', '') or ''
                full = (first + ' ' + last).strip() or getattr(entity, 'username', '') or '<noname>'
                row = {
                    'id': entity.id,
                    'title': full,
                    'username': getattr(entity, 'username', None),
                    'is_contact': bool(getattr(entity, 'contact', False)),
                    'is_verified': bool(getattr(entity, 'verified', False)),
                    'is_premium': bool(getattr(entity, 'premium', False)),
                    'unread': getattr(dialog, 'unread_count', 0),
                    'phone': getattr(entity, 'phone', None),
                }
                if getattr(entity, 'bot', False):
                    bots.append(row)
                else:
                    dms.append(row)

        groups.sort(key=lambda r: (r['title'] or '').lower())
        channels.sort(key=lambda r: (r['title'] or '').lower())
        dms.sort(key=lambda r: (-(r['unread'] or 0), (r['title'] or '').lower()))
        bots.sort(key=lambda r: (r['title'] or '').lower())

        return {'success': True, 'groups': groups, 'channels': channels,
                'dms': dms, 'bots': bots}

    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'groups': [], 'channels': [], 'dms': [], 'bots': []}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Action: send_message — broadcast a message from an account to a target.
# ---------------------------------------------------------------------------

async def send_message_for_account(account, target, text):
    """Send `text` to a single chat/user (`target` parsed via parse_target).

    Returns the standard {success, error, error_type, stop_account} dict.
    """
    kind, payload, original = parse_target(target)
    if kind == 'unknown':
        return {
            'success': False, 'error': f"Noto'g'ri target: {original!r}",
            'error_type': 'InvalidTarget', 'stop_account': False,
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

        if kind == 'private_link':
            return {
                'success': False,
                'error': "Maxfiy invite link uchun avval join_channel ishlating",
                'error_type': 'PrivateInviteUnsupported',
                'stop_account': False,
            }

        entity = await client.get_entity(payload)
        await client.send_message(entity, (text or '')[:4096])
        return {'success': True, 'error': '', 'error_type': '', 'stop_account': False}

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
    except (ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError) as e:
        return {'success': False, 'error': "Target topilmadi yoki yopiq",
                'error_type': type(e).__name__, 'stop_account': False}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Action: update_profile — set first_name, last_name, bio, username.
# ---------------------------------------------------------------------------

async def update_profile_for_account(account, *,
                                     first_name=None, last_name=None,
                                     about=None, username=None):
    """Update the account's Telegram profile.

    Any field passed as None is left unchanged. `username` set to '' clears
    the username. On username conflict (taken/invalid) we still report the
    profile fields as success but include a `username_status` flag.
    """
    from telethon.tl.functions.account import (
        UpdateProfileRequest, UpdateUsernameRequest,
    )
    from telethon.errors import UsernameOccupiedError
    try:
        from telethon.errors import UsernameInvalidError as _UNI
    except ImportError:
        _UNI = Exception  # fallback if rename happens upstream

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

        if first_name is not None or last_name is not None or about is not None:
            kwargs = {}
            if first_name is not None:
                kwargs['first_name'] = (first_name or '')[:64]
            if last_name is not None:
                kwargs['last_name'] = (last_name or '')[:64]
            if about is not None:
                kwargs['about'] = (about or '')[:140]
            await client(UpdateProfileRequest(**kwargs))

        username_status = 'unchanged'
        if username is not None:
            try:
                await client(UpdateUsernameRequest(username=username))
                username_status = 'set' if username else 'cleared'
            except UsernameOccupiedError:
                username_status = 'occupied'
            except _UNI:
                username_status = 'invalid'

        try:
            updates = {}
            if first_name is not None:
                updates['first_name'] = first_name
            if last_name is not None:
                updates['last_name'] = last_name
            if username is not None and username_status == 'set':
                updates['username'] = username
            if updates:
                await Account.objects.filter(pk=account.pk).aupdate(**updates)
        except Exception:
            pass

        return {'success': True, 'error': '', 'error_type': '',
                'username_status': username_status, 'stop_account': False}

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
        return {'success': False, 'error': str(e)[:200],
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Action: view_stories — read stories from subscribed peers, optionally react.
# ---------------------------------------------------------------------------

async def view_and_react_stories_for_account(account, *,
                                             react_chance=0.0,
                                             max_peers=50,
                                             reactions=None):
    """Mark stories from subscribed peers as seen, optionally react with a
    random emoji from `reactions` (default: like/heart/fire).
    """
    from telethon.tl.functions.stories import (
        GetAllStoriesRequest, ReadStoriesRequest,
        SendReactionRequest as StoriesSendReactionRequest,
    )

    if reactions is None:
        reactions = ['👍', '❤️', '🔥']
    rng = random.SystemRandom()

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

        peers_seen = stories_seen = reactions_sent = errors = 0

        try:
            res = await client(GetAllStoriesRequest(include_hidden=False))
        except Exception as e:
            return {'success': False, 'error': f"Stories olinmadi: {e}",
                    'error_type': type(e).__name__, 'stop_account': False}

        peer_stories = getattr(res, 'peer_stories', None) or []
        for ps in peer_stories[:max_peers]:
            peers_seen += 1
            stories = getattr(ps, 'stories', None) or []
            ids = [getattr(s, 'id', None) for s in stories if getattr(s, 'id', None) is not None]
            if not ids:
                continue
            try:
                await client(ReadStoriesRequest(peer=ps.peer, max_id=max(ids)))
                stories_seen += len(ids)
            except FloodWaitError:
                raise
            except Exception:
                errors += 1
                continue

            if react_chance > 0:
                for sid in ids:
                    if rng.random() >= react_chance:
                        continue
                    emoji = rng.choice(reactions)
                    try:
                        await client(StoriesSendReactionRequest(
                            peer=ps.peer, story_id=sid,
                            reaction=ReactionEmoji(emoticon=emoji),
                        ))
                        reactions_sent += 1
                        await asyncio.sleep(rng.uniform(0.5, 2.0))
                    except FloodWaitError:
                        raise
                    except Exception:
                        errors += 1

            await asyncio.sleep(rng.uniform(0.3, 1.0))

        return {
            'success': True, 'error': '', 'error_type': '', 'stop_account': False,
            'peers_seen': peers_seen, 'stories_seen': stories_seen,
            'reactions_sent': reactions_sent, 'errors': errors,
        }

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Action: mark_all_read — flush every unread dialog.
# ---------------------------------------------------------------------------

async def mark_all_read_for_account(account, *, max_dialogs=500):
    """Iterate dialogs and read every unread one. Returns counts."""
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

        read = skipped = 0
        n = 0
        async for dialog in client.iter_dialogs():
            n += 1
            if n > max_dialogs:
                break
            if not getattr(dialog, 'unread_count', 0):
                skipped += 1
                continue
            try:
                await client.send_read_acknowledge(dialog)
                read += 1
                await asyncio.sleep(random.uniform(0.1, 0.4))
            except FloodWaitError:
                raise
            except Exception:
                skipped += 1

        return {'success': True, 'error': '', 'error_type': '', 'stop_account': False,
                'read': read, 'skipped': skipped}

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Action: set_2fa_password — set or change the cloud (2FA) password.
# ---------------------------------------------------------------------------

async def set_2fa_password_for_account(account, *,
                                       new_password,
                                       hint='',
                                       current_password=None):
    """Set or change the account's 2FA cloud password.

    `current_password` is required when the account already has one set.
    If omitted, we try the value stored in Account.two_fa_password
    (auto-decrypted by EncryptedTextField). On success, we mirror the
    new password back to the same column.
    """
    if current_password is None:
        rec = await Account.objects.filter(pk=account.pk).afirst()
        current_password = rec.two_fa_password if rec else None

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

        await client.edit_2fa(
            current_password=current_password or None,
            new_password=new_password,
            hint=hint or '',
        )

        # Persist new password (Fernet-encrypted on save)
        try:
            obj = await Account.objects.filter(pk=account.pk).afirst()
            if obj:
                obj.two_fa_password = new_password
                await obj.asave(update_fields=['two_fa_password'])
        except Exception:
            pass

        return {'success': True, 'error': '', 'error_type': '', 'stop_account': False}

    except FloodWaitError:
        raise
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'error_type': type(e).__name__, 'stop_account': True}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'error_type': type(e).__name__, 'stop_account': False}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Live chat viewer + sender (for the in-app "Telegram-style" chat UI).
#
# These two helpers power /accounts/<pk>/live-chats/<chat_id>/. Unlike the
# bulk runners, they're called inline from a request handler — so they
# return rendered-friendly dicts instead of the runner status structs, and
# they only ever touch ONE chat per call (no looping over accounts).
# ---------------------------------------------------------------------------

async def _resolve_chat_entity(client, chat_id):
    """Find a dialog whose entity.id matches `chat_id`, or None.

    Iterating dialogs is the most reliable way to resolve an int id to a
    full entity (Telethon caches the access_hash internally during the
    walk). Faster paths like `client.get_entity(int)` require the entity
    to already be in the session's local cache, which fails on a freshly
    connected client.
    """
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if getattr(ent, 'id', None) == chat_id:
            return ent, dialog
    return None, None


def _serialize_message(m, *, chat_username, is_broadcast, chat_title):
    """Convert a Telethon Message into a JSON-friendly dict for the UI."""
    sender_name = ''
    sender_id = None
    if m.sender:
        sender_id = getattr(m.sender, 'id', None)
        first = getattr(m.sender, 'first_name', '') or ''
        last = getattr(m.sender, 'last_name', '') or ''
        sender_name = (first + ' ' + last).strip() or getattr(m.sender, 'username', '') or ''
    elif getattr(m, 'post', False):
        sender_name = chat_title

    text = m.message or ''

    replies = 0
    if getattr(m, 'replies', None):
        replies = getattr(m.replies, 'replies', 0) or 0

    post_link = ''
    if is_broadcast and chat_username:
        post_link = f'https://t.me/{chat_username}/{m.id}'

    reply_to = None
    if getattr(m, 'reply_to', None) and getattr(m.reply_to, 'reply_to_msg_id', None):
        reply_to = {'id': int(m.reply_to.reply_to_msg_id), 'text': '', 'sender': ''}

    media = None
    if m.media is not None:
        media = _serialize_media(m)

    poll = _serialize_poll(m) if isinstance(m.media, MessageMediaPoll) else None

    reactions = _serialize_reactions(m)

    buttons = _serialize_buttons(m)

    return {
        'id': m.id,
        'text': text[:4096],
        'date_iso': m.date.isoformat() if m.date else '',
        'date_human': m.date.strftime('%H:%M · %d.%m.%Y') if m.date else '',
        'out': bool(m.out),
        'sender': sender_name[:64],
        'sender_id': sender_id,
        'replies': replies,
        'post_link': post_link,
        'reply_to': reply_to,
        'media': media,
        'poll': poll,
        'reactions': reactions,
        'buttons': buttons,
        'edited': bool(getattr(m, 'edit_date', None)),
        'pinned': bool(getattr(m, 'pinned', False)),
        'via_bot': getattr(getattr(m, 'via_bot', None), 'username', None) if getattr(m, 'via_bot', None) else None,
    }


def _serialize_media(m):
    """Return {kind, mime, width, height, duration, file_size, has_thumb}."""
    media = m.media
    if isinstance(media, MessageMediaPhoto):
        return {'kind': 'photo', 'has_thumb': True}
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return {'kind': 'document'}
        mime = getattr(doc, 'mime_type', '') or ''
        attrs = getattr(doc, 'attributes', []) or []
        kind = 'document'
        width = height = duration = None
        is_animated = False
        is_sticker = False
        is_voice = False
        is_round = False
        for a in attrs:
            if isinstance(a, DocumentAttributeAnimated):
                is_animated = True
            if isinstance(a, DocumentAttributeSticker):
                is_sticker = True
            if isinstance(a, DocumentAttributeVideo):
                width = a.w; height = a.h; duration = a.duration
                is_round = bool(getattr(a, 'round_message', False))
            if isinstance(a, DocumentAttributeAudio):
                duration = a.duration
                is_voice = bool(getattr(a, 'voice', False))
        if is_sticker:
            kind = 'sticker'
        elif is_voice:
            kind = 'voice'
        elif mime.startswith('video/') or width:
            kind = 'video_round' if is_round else ('gif' if is_animated else 'video')
        elif mime.startswith('audio/'):
            kind = 'audio'
        elif mime.startswith('image/'):
            kind = 'photo'
        return {
            'kind': kind, 'mime': mime,
            'width': width, 'height': height,
            'duration': duration,
            'file_size': getattr(doc, 'size', None),
            'has_thumb': bool(getattr(doc, 'thumbs', None)),
        }
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, 'webpage', None)
        if wp is None or not getattr(wp, 'url', None):
            return None
        return {
            'kind': 'webpage',
            'url': wp.url,
            'title': (getattr(wp, 'title', '') or '')[:200],
            'description': (getattr(wp, 'description', '') or '')[:300],
            'site_name': getattr(wp, 'site_name', None),
            'has_thumb': bool(getattr(wp, 'photo', None)),
        }
    if isinstance(media, MessageMediaContact):
        return {
            'kind': 'contact',
            'first_name': getattr(media, 'first_name', ''),
            'last_name': getattr(media, 'last_name', ''),
            'phone_number': getattr(media, 'phone_number', ''),
        }
    if isinstance(media, MessageMediaGeo):
        geo = getattr(media, 'geo', None)
        if geo is None:
            return None
        return {'kind': 'geo', 'lat': getattr(geo, 'lat', None), 'long': getattr(geo, 'long', None)}
    return {'kind': 'other'}


def _serialize_poll(m):
    poll_obj = m.media.poll
    results = m.media.results
    total_voters = getattr(results, 'total_voters', 0) or 0
    chosen_set = set()
    correct_set = set()
    if results and getattr(results, 'results', None):
        for i, r in enumerate(results.results):
            if getattr(r, 'chosen', False):
                chosen_set.add(i)
            if getattr(r, 'correct', False):
                correct_set.add(i)

    options = []
    voters_by_index = {}
    if results and getattr(results, 'results', None):
        for i, r in enumerate(results.results):
            voters_by_index[i] = getattr(r, 'voters', 0) or 0

    for i, ans in enumerate(poll_obj.answers):
        opt_text = ans.text.text if hasattr(ans.text, 'text') else str(ans.text)
        options.append({
            'index': i,
            'text': opt_text,
            'voters': voters_by_index.get(i, 0),
            'chosen': i in chosen_set,
            'correct': i in correct_set,
        })

    question_text = poll_obj.question.text if hasattr(poll_obj.question, 'text') else str(poll_obj.question)
    return {
        'question': question_text,
        'options': options,
        'total_voters': total_voters,
        'closed': bool(getattr(poll_obj, 'closed', False)),
        'multiple_choice': bool(getattr(poll_obj, 'multiple_choice', False)),
        'quiz': bool(getattr(poll_obj, 'quiz', False)),
        'voted': len(chosen_set) > 0,
    }


def _serialize_reactions(m):
    rxs = getattr(m, 'reactions', None)
    if not rxs:
        return []
    out = []
    for r in (getattr(rxs, 'results', None) or []):
        emoji = ''
        kind = 'emoji'
        if isinstance(r.reaction, ReactionEmoji):
            emoji = r.reaction.emoticon or ''
        elif isinstance(r.reaction, ReactionCustomEmoji):
            kind = 'custom'
            emoji = '⭐'
        out.append({
            'emoji': emoji,
            'kind': kind,
            'count': r.count,
            'chosen': bool(getattr(r, 'chosen_order', None) is not None or getattr(r, 'chosen', False)),
        })
    return out


def _serialize_buttons(m):
    markup = getattr(m, 'reply_markup', None)
    if not markup or not getattr(markup, 'rows', None):
        return []
    out = []
    for row_i, row in enumerate(markup.rows):
        row_btns = []
        for col_i, btn in enumerate(row.buttons):
            entry = {
                'row': row_i, 'col': col_i,
                'text': getattr(btn, 'text', '') or '',
                'type': 'unknown',
            }
            if isinstance(btn, KeyboardButtonCallback):
                entry['type'] = 'callback'
            elif isinstance(btn, KeyboardButtonUrl):
                entry['type'] = 'url'
                entry['url'] = btn.url
            elif isinstance(btn, KeyboardButtonUrlAuth):
                entry['type'] = 'url'
                entry['url'] = btn.url
            elif isinstance(btn, KeyboardButtonSwitchInline):
                entry['type'] = 'switch_inline'
                entry['query'] = getattr(btn, 'query', '')
            elif isinstance(btn, KeyboardButtonGame):
                entry['type'] = 'game'
            elif isinstance(btn, KeyboardButtonBuy):
                entry['type'] = 'buy'
            elif isinstance(btn, (KeyboardButtonRequestPhone, KeyboardButtonRequestGeoLocation)):
                entry['type'] = 'request'
            row_btns.append(entry)
        out.append(row_btns)
    return out


async def fetch_chat_messages_for_account(account, chat_id, *, limit=40, after_id=0,
                                           mark_read=True):
    """Pull recent messages from a single chat with rich content.

    Set `mark_read=True` (default) so the live-chat view marks dialogs
    read on every fetch — Telegram interprets a session that views a
    chat without acknowledging reads as a scraper, and may flag it.
    """
    chat_id = int(chat_id)
    after_id = int(after_id or 0)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': f"Ulanib bo'lmadi: {e}",
                'chat': None, 'messages': []}
    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {'success': False, 'error': "Sessiya yaroqsiz",
                    'chat': None, 'messages': []}

        entity, dialog = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False,
                    'error': "Chat topilmadi",
                    'chat': None, 'messages': []}

        if isinstance(entity, Channel):
            is_megagroup = bool(getattr(entity, 'megagroup', False))
            is_broadcast = not is_megagroup
            kind = 'channel' if is_broadcast else 'group'
            is_creator = bool(getattr(entity, 'creator', False))
            is_admin = bool(getattr(entity, 'admin_rights', None))
            username = getattr(entity, 'username', None)
            title = getattr(entity, 'title', None) or '<noname>'
            members = getattr(entity, 'participants_count', None)
        elif isinstance(entity, Chat):
            is_broadcast = False
            kind = 'group'
            is_creator = bool(getattr(entity, 'creator', False))
            is_admin = bool(getattr(entity, 'admin_rights', None))
            username = None
            title = getattr(entity, 'title', None) or '<noname>'
            members = getattr(entity, 'participants_count', None)
        elif isinstance(entity, User):
            is_broadcast = False
            kind = 'bot' if getattr(entity, 'bot', False) else 'dm'
            is_creator = False
            is_admin = False
            username = getattr(entity, 'username', None)
            first = getattr(entity, 'first_name', '') or ''
            last = getattr(entity, 'last_name', '') or ''
            title = (first + ' ' + last).strip() or username or '<noname>'
            members = None
        else:
            return {'success': False, 'error': "Noma'lum chat turi",
                    'chat': None, 'messages': []}

        has_comments = False
        if kind == 'channel':
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(entity))
                has_comments = bool(getattr(full.full_chat, 'linked_chat_id', None))
            except Exception:
                pass

        if kind == 'channel':
            can_send = is_creator or is_admin
        else:
            can_send = True

        iter_kwargs = {'limit': limit}
        if after_id:
            iter_kwargs['min_id'] = after_id

        msgs = []
        async for m in client.iter_messages(entity, **iter_kwargs):
            msgs.append(_serialize_message(
                m, chat_username=username, is_broadcast=is_broadcast,
                chat_title=title,
            ))

        msgs.reverse()

        if mark_read and msgs:
            try:
                await client.send_read_acknowledge(entity, clear_mentions=True)
            except Exception:
                pass

        return {
            'success': True, 'error': '',
            'chat': {
                'id': chat_id,
                'title': title,
                'kind': kind,
                'username': username,
                'members': members,
                'is_creator': is_creator,
                'is_admin': is_admin,
                'can_send': can_send,
                'has_comments': has_comments,
                'unread': getattr(dialog, 'unread_count', 0) if dialog else 0,
            },
            'messages': msgs,
        }

    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan",
                'chat': None, 'messages': []}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200],
                'chat': None, 'messages': []}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_chat_message_for_account(account, chat_id, text, *,
                                         comment_to=None, reply_to=None):
    """Post a message. `reply_to` quotes a message in-chat; `comment_to`
    routes into a channel's linked discussion thread (Telethon-specific)."""
    chat_id = int(chat_id)
    text = (text or '').strip()
    if not text:
        return {'success': False, 'error': "Xabar bo'sh"}
    if len(text) > 4096:
        text = text[:4096]

    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': f"Ulanib bo'lmadi: {e}"}
    try:
        me = await client.get_me()
        if not me:
            await _mark_session_dead(account.pk)
            return {'success': False, 'error': "Sessiya yaroqsiz"}

        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False, 'error': "Chat topilmadi"}

        kwargs = {}
        if comment_to is not None:
            kwargs['comment_to'] = int(comment_to)
        if reply_to is not None:
            kwargs['reply_to'] = int(reply_to)

        sent = await client.send_message(entity, text, **kwargs)
        try:
            await client.send_read_acknowledge(entity)
        except Exception:
            pass
        return {'success': True, 'error': '', 'message_id': getattr(sent, 'id', None)}

    except FloodWaitError as e:
        return {'success': False, 'error': f"FloodWait: {e.seconds}s kutish kerak"}
    except SESSION_DEAD_EXCEPTIONS as e:
        await _mark_session_dead(account.pk)
        return {'success': False, 'error': "Sessiya chiqarib yuborilgan"}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def mark_chat_read_for_account(account, chat_id):
    """Acknowledge unread messages in a chat — used after every fetch so
    Telegram doesn't flag the session as a stealth scraper."""
    chat_id = int(chat_id)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': str(e)}
    try:
        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False, 'error': "Chat topilmadi"}
        await client.send_read_acknowledge(entity, clear_mentions=True)
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def react_to_chat_message_for_account(account, chat_id, msg_id, emoji):
    """Toggle a reaction on a message. emoji='' clears the user's reaction."""
    chat_id = int(chat_id); msg_id = int(msg_id)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': str(e)}
    try:
        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False, 'error': "Chat topilmadi"}
        reaction = [ReactionEmoji(emoticon=emoji)] if emoji else []
        await client(SendReactionRequest(
            peer=entity, msg_id=msg_id, reaction=reaction,
            add_to_recent=bool(emoji),
        ))
        return {'success': True}
    except ReactionInvalidError:
        return {'success': False, 'error': "Bu emoji bu chatda ruxsat etilmagan"}
    except FloodWaitError as e:
        return {'success': False, 'error': f"FloodWait: {e.seconds}s"}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def vote_in_chat_poll_for_account(account, chat_id, msg_id, option_indices):
    """Cast a vote on a poll. `option_indices` is a list of option ints."""
    chat_id = int(chat_id); msg_id = int(msg_id)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': str(e)}
    try:
        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False, 'error': "Chat topilmadi"}
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg or not isinstance(msg.media, MessageMediaPoll):
            return {'success': False, 'error': "So'rovnoma topilmadi"}
        answers = msg.media.poll.answers
        try:
            options = [answers[i].option for i in option_indices if 0 <= i < len(answers)]
        except Exception:
            return {'success': False, 'error': "Variant noto'g'ri"}
        if not options:
            return {'success': False, 'error': "Variant tanlanmagan"}
        await client(SendVoteRequest(peer=entity, msg_id=msg_id, options=options))
        return {'success': True}
    except FloodWaitError as e:
        return {'success': False, 'error': f"FloodWait: {e.seconds}s"}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def click_bot_button_in_chat_for_account(account, chat_id, msg_id, row, col):
    """Click an inline-keyboard button on a message (callback / url / etc.).

    Telethon's `Message.click(i, j)` handles dispatch by button type:
    callback → SendCallbackQuery, url → returns the URL, switch_inline → opens
    the inline picker (we just return the query string here).
    """
    chat_id = int(chat_id); msg_id = int(msg_id)
    row = int(row); col = int(col)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return {'success': False, 'error': str(e)}
    try:
        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return {'success': False, 'error': "Chat topilmadi"}
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            return {'success': False, 'error': "Xabar topilmadi"}
        try:
            result = await msg.click(row, col)
        except BotResponseTimeoutError:
            return {'success': False, 'error': "Bot javob bermadi (timeout)"}
        text = ''
        if result is not None:
            text = getattr(result, 'message', None) or str(result)
        return {'success': True, 'result': text[:300]}
    except FloodWaitError as e:
        return {'success': False, 'error': f"FloodWait: {e.seconds}s"}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def download_chat_message_media_for_account(account, chat_id, msg_id, *, thumb=True):
    """Download photo/document/sticker bytes for a single message.

    Returns (bytes, mime_type) on success or (None, error_str) on failure.
    `thumb=True` fetches the smallest stripped/jpeg preview only — fast,
    safe to call from the live view. `thumb=False` downloads the full
    file (used by the user's "ko'rish" click).
    """
    from io import BytesIO
    chat_id = int(chat_id); msg_id = int(msg_id)
    try:
        client = await get_client_for_account(account)
    except Exception as e:
        return None, str(e)
    try:
        entity, _ = await _resolve_chat_entity(client, chat_id)
        if entity is None:
            return None, "Chat topilmadi"
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg or not msg.media:
            return None, "Media yo'q"

        bio = BytesIO()
        if thumb:
            try:
                await client.download_media(msg, bio, thumb=-1)
            except Exception:
                bio = BytesIO()
                await client.download_media(msg, bio)
        else:
            await client.download_media(msg, bio)
        bio.seek(0)
        data = bio.read()
        if not data:
            return None, "Bo'sh fayl"

        mime = 'image/jpeg'
        if isinstance(msg.media, MessageMediaDocument) and msg.media.document:
            doc_mime = getattr(msg.media.document, 'mime_type', '') or ''
            if doc_mime:
                mime = doc_mime
        return data, mime
    except FloodWaitError as e:
        return None, f"FloodWait: {e.seconds}s"
    except Exception as e:
        return None, str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
