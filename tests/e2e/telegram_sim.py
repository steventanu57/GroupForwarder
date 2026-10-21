"""A simulated Telegram server underneath Telethon's REAL client, so the real group_forwarder.py
runs end to end: Telethon's own update handling (message box, entity cache, events, album
grouping, "me") is used; only the network connection is replaced (see FakeSender).

Telegram's rules that it applies (from the MTProto API docs and known errors):
- forwarding from a chat with "Restrict saving content": CHAT_FORWARDS_RESTRICTED
- posting where you can't write: CHAT_WRITE_FORBIDDEN
- text at most 4096, captions at most 1024 (UTF-16 units): MESSAGE_TOO_LONG / MEDIA_CAPTION_TOO_LONG
- formatting entities must lie inside the text
- a topic must exist in a forum target: TOPIC_ID_INVALID
- albums have 2-10 items
- without a login, even "who am I" is refused: AUTH_KEY_UNREGISTERED
- every request and every answer must survive being turned into Telegram's binary format and back
- FLOOD_WAIT_X once, when asked (for the rate-limit test)
It also lists a forum group's topics, and has a user whose name holds terminal control codes."""

import asyncio
import inspect
import itertools
import os
import pickle
import random
import time
from datetime import UTC, datetime

import telethon
from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.extensions import BinaryReader
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.tlobject import TLObject

ME, BOB, ROBOT, EVIL = 1000, 2000, 3000, 4000
USERS = {
    ME: ("zz-Me", False),
    BOB: ("zz-Bob", False),
    ROBOT: ("zz-RobotUser", True),
    # Control codes (set the terminal's title, clear it) and a line break, to fake a log line
    EVIL: ("zz-\x1b]0;zz-HIJACKED\x07Evil\x1b[2J\n[00:00:00] zz-Fake line", False),
}


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text, offset, length):
    data = text.encode("utf-16-le")
    return data[2 * offset : 2 * (offset + length)].decode("utf-16-le")


def marked(channel_id):
    return int(f"-100{channel_id}")


def fill(cls, **given):
    """Builds a TL object, filling required arguments Telethon doesn't care about."""
    kwargs = {}
    for name, param in inspect.signature(cls.__init__).parameters.items():
        if name == "self":
            continue
        if name in given:
            kwargs[name] = given[name]
        elif param.default is inspect.Parameter.empty:
            ann = str(param.annotation)
            kwargs[name] = (
                datetime.now(UTC)
                if "datetime" in ann
                else []
                if "List" in ann
                else ""
                if "str" in ann
                else False
                if "bool" in ann
                else 0
            )
    return cls(**kwargs)


class Server:
    def __init__(self):
        self.client = None
        self.kwargs = {}  # What the forwarder passed to TelegramClient
        self.authorized = True
        self.account_pts = 10
        self.chats = {}  # channel id -> dict
        self.saved = []  # Saved Messages: list of message dicts
        self.ids = itertools.count(50000)
        self.group_ids = itertools.count(9000)
        self.log, self.refused, self.flood_once = [], [], {}
        self.delay = (
            0.0  # Random answer delay for forwards (to see the order they arrive in)
        )

    # ---------- setup ----------
    def add_chat(
        self,
        cid,
        title,
        kind="channel",
        member=True,
        can_write=True,
        noforwards=False,
        forum=False,
        topics=(),
    ):
        self.chats[cid] = {
            "id": cid,
            "title": title,
            "kind": kind,
            "member": member,
            "can_write": can_write,
            "noforwards": noforwards,
            "forum": forum,
            "topics": dict(topics)
            if isinstance(topics, dict)
            else dict.fromkeys(topics, "zz"),
            "pts": 100,
            "access_hash": random.randint(1, 2**62),
            "messages": [],
        }

    def save(self, path):
        skip = ("client", "kwargs", "log", "refused", "ids", "group_ids")
        state = {k: v for k, v in self.__dict__.items() if k not in skip}
        state["next_id"] = next(self.ids)
        state["next_group"] = next(self.group_ids)
        path.write_bytes(pickle.dumps(state))

    def load(self, path):
        # Safe to unpickle: the simulator's own state, written by the previous run of this test
        state = pickle.loads(path.read_bytes())
        self.ids = itertools.count(state.pop("next_id"))
        self.group_ids = itertools.count(state.pop("next_group"))
        self.__dict__.update(state)

    # ---------- Telegram objects ----------
    def tl_user(self, uid):
        name, bot = USERS[uid]
        return types.User(
            id=uid,
            is_self=uid == ME,
            bot=bot,
            bot_info_version=1 if bot else None,
            access_hash=uid * 7,
            first_name=name,
        )

    def tl_chat(self, c):
        return types.Channel(
            id=c["id"],
            title=c["title"],
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(UTC),
            access_hash=c["access_hash"],
            broadcast=c["kind"] == "channel",
            megagroup=c["kind"] != "channel",
            forum=c["forum"],
            noforwards=c["noforwards"],
            left=not c["member"],
        )

    def tl_message(self, m):
        peer = (
            types.PeerUser(ME) if m["chat"] == "saved" else types.PeerChannel(m["chat"])
        )
        from_id = types.PeerUser(m["from"]) if m.get("from") else None
        date = datetime.fromtimestamp(m["date"], UTC)
        if m.get("service"):
            return types.MessageService(
                id=m["id"],
                peer_id=peer,
                date=date,
                action=types.MessageActionPinMessage(),
                out=m.get("out", False),
                from_id=from_id,
            )
        media = None
        if m.get("photo"):
            photo = types.Photo(
                id=m["photo"],
                access_hash=m["photo"] * 3,
                file_reference=b"zz",
                date=datetime.now(UTC),
                sizes=[types.PhotoSize("x", 100, 100, 1000)],
                dc_id=2,
            )
            media = types.MessageMediaPhoto(photo=photo)
        fwd = None
        if m.get("fwd_from"):
            fwd = types.MessageFwdHeader(
                date=datetime.now(UTC),
                from_id=types.PeerChannel(m["fwd_from"][0]),
                channel_post=m["fwd_from"][1],
            )
        reply_to = None
        if m.get("topic"):
            reply_to = types.MessageReplyHeader(
                reply_to_msg_id=m["topic"], forum_topic=True
            )
        return types.Message(
            id=m["id"],
            peer_id=peer,
            date=date,
            message=m.get("text", ""),
            out=m.get("out", False),
            from_id=from_id,
            post=m.get("post", False),
            fwd_from=fwd,
            media=media,
            entities=m.get("entities") or None,
            grouped_id=m.get("grouped"),
            noforwards=m.get("noforwards", False),
            reply_to=reply_to,
        )

    def everyone(self):
        return [self.tl_user(u) for u in USERS], [
            self.tl_chat(c) for c in self.chats.values()
        ]

    # ---------- the world: other people, other devices ----------
    def new_message(self, cid, **m):
        chat = self.chats[cid]
        msg = {
            "id": len(chat["messages"]) + 1,
            "chat": cid,
            "date": m.pop("date", time.time()),
            **m,
        }
        chat["messages"].append(msg)
        return msg

    def post(self, cid, deliver=True, **m):
        """Someone posts in a chat. deliver=False: the forwarder is off and never gets the update."""
        album = m.pop("album", None)
        if album:
            group = next(self.group_ids)
            made = [self.new_message(cid, grouped=group, **item) for item in album]
        else:
            made = [self.new_message(cid, **m)]
        if deliver:
            chat = self.chats[cid]
            updates = []
            for msg in made:
                chat["pts"] += 1
                updates.append(
                    types.UpdateNewChannelMessage(self.tl_message(msg), chat["pts"], 1)
                )
            self.push(updates)
        return made

    def saved_command(self, text):
        """You type a command in Saved Messages on another device."""
        msg = {
            "id": next(self.ids),
            "chat": "saved",
            "date": time.time(),
            "text": text,
            "from": ME,
            "out": True,
        }
        self.saved.append(msg)
        self.account_pts += 1
        self.push([types.UpdateNewMessage(self.tl_message(msg), self.account_pts, 1)])

    def push(self, update_list):
        users, chats = self.everyone()
        self.client._updates_queue.put_nowait(
            types.Updates(
                updates=update_list,
                users=users,
                chats=chats,
                date=datetime.now(UTC),
                seq=0,
            )
        )

    # ---------- requests ----------
    def chat_of(self, peer):
        if isinstance(peer, (types.InputPeerSelf, types.InputPeerUser)) or peer is None:
            return None
        return self.chats[peer.channel_id]

    def refuse(self, error_cls, request, why):
        self.refused.append((type(request).__name__, why))
        raise error_cls(request=request)

    def check_text(self, request, text, entities, limit):
        if utf16(text or "") > limit:
            error = (
                errors.MessageTooLongError
                if limit == 4096
                else errors.MediaCaptionTooLongError
            )
            self.refuse(error, request, f"text {utf16(text)} > {limit}")
        for e in entities or []:
            if e.offset < 0 or e.length <= 0 or e.offset + e.length > utf16(text):
                self.refuse(
                    errors.EntityBoundsInvalidError, request, f"entity {e} outside text"
                )

    def check_target(self, request, chat, topic):
        if not chat["member"] or not chat["can_write"]:
            self.refuse(
                errors.ChatWriteForbiddenError,
                request,
                f"can't write in {chat['title']}",
            )
        if topic and (not chat["forum"] or topic not in chat["topics"]):
            self.refuse(
                errors.TopicIdInvalidError,
                request,
                f"no topic {topic} in {chat['title']}",
            )

    def deliver_to(self, chat, items, random_ids, topic):
        """Stores new messages in a target chat and returns the Updates Telegram would send back."""
        updates = []
        for item, rid in zip(items, random_ids, strict=True):
            msg = self.new_message(chat["id"], **item, topic=topic, out=True)
            chat["pts"] += 1
            updates += [
                types.UpdateMessageID(msg["id"], rid),
                types.UpdateNewChannelMessage(self.tl_message(msg), chat["pts"], 1),
            ]
        users, chats = self.everyone()
        return types.Updates(
            updates=updates, users=users, chats=chats, date=datetime.now(UTC), seq=0
        )

    @staticmethod
    def topic_of(reply_to):
        return getattr(reply_to, "top_msg_id", None) or getattr(
            reply_to, "reply_to_msg_id", None
        )

    async def handle(self, request):
        bytes(request)  # Must be valid in Telegram's binary format, like on the wire
        name = type(request).__name__
        self.log.append((name, request))
        if name in self.flood_once:
            self.refused.append((name, "flood wait"))
            raise errors.FloodWaitError(
                request=request, capture=self.flood_once.pop(name)
            )
        wrappers = (
            functions.InvokeWithLayerRequest,
            functions.InitConnectionRequest,
            functions.InvokeWithoutUpdatesRequest,
        )
        while isinstance(request, wrappers):
            request = request.query
            name = type(request).__name__
        method = getattr(self, "r_" + name, None)
        if method is None:
            self.refused.append((name, "not simulated"))
            raise errors.RPCError(
                request=request, message=f"NOT_SIMULATED_{name}", code=400
            )
        return await method(request)

    async def r_GetConfigRequest(self, r):
        option = types.DcOption(id=2, ip_address="149.154.167.51", port=443)
        return fill(types.Config, this_dc=2, test_mode=False, dc_options=[option])

    async def r_GetUsersRequest(self, r):
        if not self.authorized:
            raise errors.AuthKeyUnregisteredError(request=r)
        return [self.tl_user(ME)]

    async def r_GetStateRequest(self, r):
        if not self.authorized:
            raise errors.AuthKeyUnregisteredError(request=r)
        return types.updates.State(
            pts=self.account_pts, qts=0, date=datetime.now(UTC), seq=0, unread_count=0
        )

    async def r_GetDifferenceRequest(self, r):
        return types.updates.DifferenceEmpty(date=datetime.now(UTC), seq=0)

    async def r_GetChannelDifferenceRequest(self, r):
        pts = self.chats[r.channel.channel_id]["pts"]
        return types.updates.ChannelDifferenceEmpty(pts=pts, final=True)

    async def r_GetDialogsRequest(self, r):
        if r.offset_id or (r.offset_date and r.offset_date.timestamp() > 0):
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        dialogs, messages = [], []
        for c in self.chats.values():
            if not c["member"]:
                continue
            top = (
                c["messages"][-1]
                if c["messages"]
                else self.new_message(c["id"], text="zz created", service=True)
            )
            dialogs.append(
                fill(
                    types.Dialog,
                    peer=types.PeerChannel(c["id"]),
                    top_message=top["id"],
                    notify_settings=types.PeerNotifySettings(),
                )
            )
            messages.append(self.tl_message(top))
        users, chats = self.everyone()
        return types.messages.Dialogs(
            dialogs=dialogs,
            messages=messages,
            chats=[x for x in chats if not x.left],
            users=users,
        )

    async def r_GetHistoryRequest(self, r):
        chat = self.chat_of(r.peer)
        found = [
            m
            for m in reversed(chat["messages"])
            if m["id"] > r.min_id
            and (not r.offset_id or m["id"] < r.offset_id)
            and (not r.max_id or m["id"] < r.max_id)
        ]
        found = found[r.add_offset or 0 :][: r.limit]
        users, chats = self.everyone()
        return types.messages.ChannelMessages(
            pts=chat["pts"],
            count=len(chat["messages"]),
            messages=[self.tl_message(m) for m in found],
            topics=[],
            chats=chats,
            users=users,
        )

    async def r_ForwardMessagesRequest(self, r):
        source, target = self.chat_of(r.from_peer), self.chat_of(r.to_peer)
        if self.delay:
            await asyncio.sleep(random.uniform(0, self.delay))
        if source["noforwards"]:
            self.refuse(
                errors.ChatForwardsRestrictedError,
                r,
                f"{source['title']} restricts saving content",
            )
        self.check_target(r, target, r.top_msg_id)
        originals = {m["id"]: m for m in source["messages"]}
        missing = [i for i in r.id if i not in originals]
        if missing:
            self.refuse(errors.MessageIdInvalidError, r, f"no messages {missing}")
        group = next(self.group_ids) if len(r.id) > 1 else None
        items = [
            {
                "text": originals[i].get("text", ""),
                "photo": originals[i].get("photo"),
                "entities": originals[i].get("entities"),
                "fwd_from": (source["id"], i),
                "grouped": group if originals[i].get("grouped") else None,
            }
            for i in r.id
        ]
        return self.deliver_to(target, items, r.random_id, r.top_msg_id)

    async def r_SendMessageRequest(self, r):
        if isinstance(r.peer, types.InputPeerSelf):
            self.check_text(r, r.message, r.entities, 4096)
            msg = {
                "id": next(self.ids),
                "chat": "saved",
                "date": time.time(),
                "text": r.message,
                "from": ME,
                "out": True,
                "by_forwarder": True,
            }
            self.saved.append(msg)
            self.account_pts += 1
            return types.UpdateShortSentMessage(
                id=msg["id"],
                pts=self.account_pts,
                pts_count=1,
                date=datetime.now(UTC),
                out=True,
            )
        chat = self.chat_of(r.peer)
        topic = self.topic_of(r.reply_to)
        self.check_target(r, chat, topic)
        self.check_text(r, r.message, r.entities, 4096)
        item = {"text": r.message, "entities": r.entities, "copy": True}
        return self.deliver_to(chat, [item], [r.random_id], topic)

    async def r_SendMediaRequest(self, r):
        chat = self.chat_of(r.peer)
        topic = self.topic_of(r.reply_to)
        self.check_target(r, chat, topic)
        self.check_text(r, r.message, r.entities, 1024)
        if not isinstance(r.media, types.InputMediaPhoto):
            self.refuse(
                errors.MediaInvalidError,
                r,
                f"unexpected media {type(r.media).__name__}",
            )
        item = {
            "text": r.message,
            "entities": r.entities,
            "photo": r.media.id.id,
            "copy": True,
        }
        return self.deliver_to(chat, [item], [r.random_id], topic)

    async def r_GetForumTopicsRequest(self, r):
        # Telethon 1.45 sends messages.GetForumTopicsRequest(peer), older ones channels.(channel)
        peer = getattr(r, "peer", None) or r.channel
        chat = self.chats[peer.channel_id]
        if not chat["forum"]:
            self.refuse(
                errors.ChannelForumMissingError, r, f"{chat['title']} has no topics"
            )
        topics = []
        for number, title in sorted(chat["topics"].items()):
            given = {"id": number, "title": title, "top_message": number}
            for name, param in inspect.signature(
                types.ForumTopic.__init__
            ).parameters.items():
                if "PeerNotifySettings" in str(param.annotation):
                    given[name] = types.PeerNotifySettings()
                elif "TypePeer" in str(param.annotation):
                    given[name] = types.PeerChannel(chat["id"])
            topics.append(fill(types.ForumTopic, **given))
        users, chats = self.everyone()
        return fill(
            types.messages.ForumTopics,
            count=len(topics),
            topics=topics,
            messages=[],
            chats=chats,
            users=users,
            pts=chat["pts"],
        )

    async def r_SendMultiMediaRequest(self, r):
        chat = self.chat_of(r.peer)
        topic = self.topic_of(r.reply_to)
        self.check_target(r, chat, topic)
        if not 2 <= len(r.multi_media) <= 10:
            self.refuse(errors.MediaInvalidError, r, "album must have 2-10 items")
        group = next(self.group_ids)
        items = []
        for single in r.multi_media:
            self.check_text(r, single.message, single.entities, 1024)
            items.append(
                {
                    "text": single.message,
                    "entities": single.entities,
                    "photo": single.media.id.id,
                    "grouped": group,
                    "copy": True,
                }
            )
        random_ids = [s.random_id for s in r.multi_media]
        return self.deliver_to(chat, items, random_ids, topic)


class FakeSender:
    """Stands in for Telethon's MTProtoSender: requests go to the simulated server, not the network."""

    def __init__(self, server):
        self.server, self.auth_key, self._connection = (
            server,
            AuthKey(os.urandom(256)),
            None,
        )
        self._connected, self._disconnected = False, None

    async def connect(self, connection):
        self._connected = True
        self._disconnected = asyncio.get_running_loop().create_future()
        return True

    def is_connected(self):
        return self._connected

    def _transport_connected(self):
        return self._connected

    def _keepalive_ping(self, rnd_id):
        pass

    async def disconnect(self):
        self._connected = False
        if self._disconnected and not self._disconnected.done():
            self._disconnected.set_result(None)

    @property
    def disconnected(self):
        return asyncio.shield(self._disconnected)

    def send(self, request, ordered=False):
        if isinstance(request, list):
            return [self.send(r) for r in request]
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        async def answer():
            try:
                result = await self.server.handle(request)
                if isinstance(result, TLObject):
                    # Through Telegram's binary format and back, like a real answer
                    with BinaryReader(bytes(result)) as reader:
                        result = request.read_result(reader)
                # Like the real sender: answers to our own requests also update the client's state
                self._updates_queue = self.server.client._updates_queue
                MTProtoSender._store_own_updates(self, result)
                future.set_result(result)
            except Exception as e:  # noqa: BLE001 - every error goes back to the caller, like Telegram's
                future.set_exception(e)

        loop.create_task(answer())
        return future


SERVER = Server()


class SimClient(telethon.TelegramClient):
    """The real TelegramClient, with the simulated server as its network."""

    def __init__(self, session, api_id, api_hash, **kwargs):
        super().__init__(session, api_id, api_hash, **kwargs)
        self._sender = FakeSender(SERVER)
        SERVER.client = self
        SERVER.kwargs = kwargs
        self.running = asyncio.Event()

    def run_until_disconnected(self):
        self.running.set()
        return super().run_until_disconnected()
