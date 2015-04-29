'''
This file is part of Volapi.

Volapi is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Volapi is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Volapi.  If not, see <http://www.gnu.org/licenses/>.
'''
# pylint: disable=bad-continuation,too-many-lines,broad-except

import json
import os
import re
import string
import time
import warnings

import requests

from collections import OrderedDict
from collections import defaultdict
from threading import RLock
from threading import Event
from threading import get_ident as get_thread_ident

from .arbritrator import ARBITRATOR, Listeners, Protocol
from .multipart import Data
from .utils import html_to_text, random_id, to_json

__version__ = "2.0"

BASE_URL = "https://volafile.io"
BASE_ROOM_URL = BASE_URL + "/r/"
BASE_REST_URL = BASE_URL + "/rest/"
BASE_WS_URL = "wss://volafile.io/api/"
EVENT_TYPES = (
    "chat",
    "file",
    "user_count",
    "config",
    "user",
    "owner",
    "update_assets",
    "subscribed",
    "hooks",
    "time",
    "login",
    "chat_success")


class Connection(requests.Session):
    """Bundles a requests/websocket pair"""

    def __init__(self, room):
        super().__init__()

        self.room = room

        agent = "Volafile-API/{}".format(__version__)

        self.headers.update({"User-Agent": agent})

        self.lock = RLock()
        self.listeners = defaultdict(lambda: defaultdict(Listeners))
        self.must_process = False
        self._queues_enabled = True

        self._ping_interval = 20  # default

        ws_url = ("{}?rn={}&EIO=3&transport=websocket&t={}".
                  format(BASE_WS_URL, random_id(6),
                         int(time.time() * 1000)))
        self.proto = Protocol(self)
        ARBITRATOR.create_connection(self.proto, ws_url, agent)

    @property
    def ping_interval(self):
        """Gets the ping interval"""

        return self._ping_interval

    @property
    def connected(self):
        """Connection state"""

        return self.proto.connected

    def send_message(self, payload):
        """Send a message"""

        ARBITRATOR.send_message(self.proto, payload)

    def make_call(self, fun, args):
        """Makes a regular API call"""

        obj = {"fn": fun, "args": args}
        obj = [self.proto.max_id, [[0, ["call", obj]], self.proto.send_count]]
        self.send_message("4" + to_json(obj))
        self.proto.send_count += 1

    def close(self):
        """Closes connection pair"""

        ARBITRATOR.close(self.proto)
        super().close()
        del self.room

    def subscribe(self, room_name, username, secret_key):
        """Make subscribe API call"""

        checksum, checksum2 = self._get_checksums(room_name)
        subscribe_options = {"room": room_name,
                             "checksum": checksum,
                             "checksum2": checksum2,
                             "nick": username
                             }
        if secret_key:
            subscribe_options['secretToken'] = secret_key
        obj = [-1, [[0, ["subscribe", subscribe_options]],
                    0]]
        self.send_message("4" + to_json(obj))

    def on_message(self, new_data):
        """Processes incoming messages"""

        if new_data[0] == '0':
            json_data = json.loads(new_data[1:])
            self._ping_interval = float(
                json_data['pingInterval']) / 1000
            return

        if new_data[0] == '1':
            self.close()
            return

        if new_data[0] == '4' and hasattr(self, "room"):
            json_data = json.loads(new_data[1:])
            if isinstance(json_data, list) and len(json_data) > 1:
                self.proto.max_id = int(json_data[1][-1])
                self.room.add_data(json_data)
            return

    def _get_checksums(self, room_name):
        """Gets the main checksums"""

        try:
            text = self.get(
                "https://static.volafile.io/static/js/main.js?c=" + self.room.cs2).text
            cs1 = re.search(r'config\.checksum\s*=\s*"(\w+?)"', text).group(1)

            return cs1, self.room.cs2
        except Exception:
            raise IOError("Failed to get checksums")

    def add_listener(self, event_type, callback):
        """Add a listener for specific event type.
        You'll need to actually listen for changes using the listen method"""

        if event_type not in EVENT_TYPES:
            raise ValueError("Invalid event type: {}".format(event_type))
        thread = get_thread_ident()
        with self.lock:
            listener = self.listeners[event_type][thread]
        listener.add(callback)
        if event_type == "file":
            for file in self.room.files:
                self.enqueue_data(event_type, file)
        self.process_queues()

    def enqueue_data(self, event_type, data):
        """Enqueue a data item for specific event type"""

        with self.lock:
            listeners = list(self.listeners[event_type].values())
            for listener in listeners:
                listener.enqueue(data)
                self.must_process = True

    @property
    def queues_enabled(self):
        """Whether queue processing is enabled"""

        return self._queues_enabled

    @queues_enabled.setter
    def queues_enabled(self, value):
        """Sets whether queue processing is enabled"""

        with self.lock:
            self._queues_enabled = value

    def process_queues(self, forced=False):
        """Process queues if any have data queued"""

        with self.lock:
            if (not forced and not self.must_process) or \
                    not self._queues_enabled:
                return
            with ARBITRATOR.condition:
                ARBITRATOR.condition.notify_all()
            self.must_process = False

    @property
    def _listeners_for_thread(self):
        """All Listeners for the current thread"""

        thread = get_thread_ident()
        with self.lock:
            return [l for m in self.listeners.values()
                    for (tid, l) in m.items() if tid == thread]

    def validate_listeners(self):
        """Validates that some listeners are actually registered"""

        listeners = self._listeners_for_thread
        if not sum(len(l) for l in listeners):
            raise ValueError("No active listeners")

    def listen(self):
        """Listen for changes in all registered listeners."""

        self.validate_listeners()
        with ARBITRATOR.condition:
            while self.connected:
                ARBITRATOR.condition.wait()
                if not self.run_queues():
                    break

    def run_queues(self):
        """Run all queues that have data queued"""

        listeners = self._listeners_for_thread
        return sum(l.process() for l in listeners) > 0


class Room:
    """ Use this to interact with a room as a user
    Example:
        with Room("BEEPi", "SameDiversePerson") as r:
            r.post_chat("Hello, world!")
            r.upload_file("onii-chan.ogg")
    """
    # pylint: disable=unused-argument

    def __init__(self, name=None, user=None, subscribe=True):
        """name is the room name, if none then makes a new room
        user is your user name, if none then generates one for you"""

        self.conn = Connection(self)

        room_resp = None
        self.name = name
        if not self.name:
            room_resp = self.conn.get(BASE_URL + "/new")
            url = room_resp.url
            try:
                self.name = re.search(r'r/(.+?)$', url).group(1)
            except Exception:
                raise IOError("Failed to create room")

        self._config = {}
        try:
            if not room_resp:
                room_resp = self.conn.get(BASE_ROOM_URL + self.name)

            text = room_resp.text
            text = text.replace('\n', '')

            self.cs2 = re.search(r'checksum2\s*:\s*"(\w+?)"', text).group(1)

            text = re.sub(
                r'(\w+):(?=([^"\\]*(\\.|"([^"\\]*\\.)*[^"\\]*"))*[^"]*$)',
                r'"\1":',
                text)
            text = text.replace('true', '"true"').replace('false', '"false"')
            text = re.search(r'config=({.+});', text).group(1)
            config = json.loads(text)

            self._config['title'] = config['name']
            self._config['private'] = config.get('private', "true") == 'true'
            self._config['motd'] = config.get('motd')
            secret_key = config.get('secretToken')

            self._config['max_title'] = config['max_room_name_length']
            self._config['max_message'] = config['chat_max_message_length']
            max_nick = config['chat_max_alias_length']
            self._config['max_nick'] = max_nick
            self._config['max_file'] = config['file_max_size']
            self._config['ttl'] = config.get('file_ttl')
            if self._config['ttl'] is None:
                self._config['ttl'] = config['file_time_to_live']
            else:
                # convert hours to seconds
                self._config['ttl'] *= 3600
            self._config['session_lifetime'] = config['session_lifetime']

        except Exception:
            raise IOError("Failed to get room config for {}".format(self.name))

        if not subscribe and not user:
            user = random_id(6)
        self.user = User(user, self.conn, max_nick)
        self.owner = bool(secret_key)

        self._user_count = 0
        self._files = OrderedDict()
        self._filereqs = {}

        if subscribe:
            self.conn.subscribe(self.name, self.user.name, secret_key)

    def __repr__(self):
        return ("<Room({},{},connected={})>".
                format(self.name, self.user.name, self.connected))

    def __enter__(self):
        return self

    def __exit__(self, extype, value, traceback):
        self.close()

    @property
    def connected(self):
        """Room is connected"""

        return self.conn.connected

    def add_listener(self, event_type, callback):
        """Add a listener for specific event type.
        You'll need to actually listen for changes using the listen method"""

        return self.conn.add_listener(event_type, callback)

    def listen(self, onmessage=None, onfile=None, onusercount=None):
        """Listen for changes in all registered listeners.
        Please note that the on* arguments are present solely for legacy
        purposes. New code should use add_listener."""

        if onmessage:
            self.add_listener("chat", onmessage)
        if onfile:
            self.add_listener("file", onfile)
        if onusercount:
            self.add_listener("user_count", onusercount)
        return self.conn.listen()

    def _handle_user_count(self, data, data_type):
        """Handle user count changes"""

        self._user_count = data
        self.conn.enqueue_data("user_count", self._user_count)

    def _handle_files(self, data, data_type):
        """Handle new files being uploaded"""

        files = data['files']
        for file in files:
            file = File(self.conn, file[0], file[1],
                        type=file[2],
                        size=file[3],
                        expire_time=int(file[4]) / 1000,
                        uploader=file[6]['user'])
            self._files[file.id] = file
            self.conn.enqueue_data("file", file)

    def _handle_delete_file(self, data, data_type):
        """Handle files being removed"""

        # XXX: Should this notify the file listener as well,
        # and if so, how?
        del self._files[data]

    def _handle_chat(self, data, data_type):
        """Handle chat messages"""

        files = []
        rooms = {}
        msg = ""
        html_msg = ""

        for part in data["message"]:
            ptype = part['type']
            if ptype == 'text':
                msg += part['value']
                html_msg += part['value']
            elif ptype == 'break':
                msg += "\n"
                html_msg += "\n"
            elif ptype == 'file':
                if part['id'] in self._files:
                    files += self._files[part['id']],
                else:
                    new_file = File(self.conn, part['id'], part['name'])
                    files += new_file,
                    self._filereqs[part['id']] = new_file
                msg += "@" + part['id']
                html_msg += "@" + part['id']
            elif ptype == 'room':
                rooms[part["id"]] = part['name']
                msg += "#" + part['id']
                html_msg += "#" + part['id']
            elif ptype == 'url':
                msg += part['text']
                html_msg += part['text']
            elif ptype == 'raw':
                msg += html_to_text(part['value'])
                html_msg += part['value']
            else:
                warnings.warn(
                    "unknown message type '{}'".format(ptype),
                    Warning)

        options = data['options']
        admin = 'admin' in options
        user = 'user' in options or admin

        chat_message = ChatMessage(data["nick"], msg,
                                   files=files,
                                   rooms=rooms,
                                   html_msg=html_msg,
                                   logged_in=user,
                                   donor="donator" in options,
                                   admin=admin)
        self.conn.enqueue_data("chat", chat_message)

    def _handle_changed_config(self, change, data_type):
        """Handle configuration changes"""

        try:
            if change['key'] == 'name':
                self._config['title'] = change['value']
                return
            if change['key'] == 'file_ttl':
                self._config['ttl'] = change['value'] * 3600
                return
            if change['key'] == 'private':
                self._config['private'] = change['value']
                return
            if change['key'] == 'motd':
                self._config['motd'] = change.get('value', "")
                return

            warnings.warn("unknown config key '{}'".
                          format(change['key']),
                          Warning)

        finally:
            self.conn.enqueue_data("config", self)

    def _handle_chat_name(self, data, data_type):
        """Handle user name changes"""

        self.user.name = data
        self.conn.enqueue_data("user", self.user)

    def _handle_owner(self, data, data_type):
        """Handle room owner changes"""

        self.owner = data['owner']
        self.conn.enqueue_data("owner", self.owner)

    def _handle_fileinfo(self, data, data_type):
        """Handle file information responses"""

        file = self._files.get(data["id"])
        if not file:
            file = self._filereqs.get(data["id"])
            if file:
                del self._filereqs[data["id"]]
        if file:
            file.add_info(data)

    def _handle_time(self, data, data_type):
        """Handle time changes"""

        self.conn.enqueue_data("time", data / 1000)

    def _handle_submitChat(self, data, data_type):
        """Handle successfully submitted chat message notifications"""

        # pylint: disable=invalid-name
        # for compat reasons
        self.conn.enqueue_data("chat_success", data)
        self.conn.enqueue_data("submitChat", data)

    def _handle_generic(self, data, data_type):
        """Handle generic notifications"""

        self.conn.enqueue_data(data_type, data)

    _handle_update_assets = _handle_generic
    _handle_subscribed = _handle_generic
    _handle_hooks = _handle_generic
    _handle_login = _handle_generic
    _handle_room_old = _handle_generic

    def _handle_unhandled(self, data, data_type):
        """Handle life, the universe and the rest"""

        if not self:
            raise ValueError(self)
        warnings.warn("unknown data type '{}' with data '{}'".
                      format(data_type, data),
                      Warning)

    def add_data(self, rawdata):
        """Add data to given room's state"""

        for item in rawdata[1:]:
            try:
                data_type = item[0][1][0]
            except IndexError:
                data_type = None
            try:
                data = item[0][1][1]
            except IndexError:
                data = dict()

            method = getattr(self, "_handle_" + data_type,
                             self._handle_unhandled)
            method(data, data_type)
        self.conn.process_queues()

    @property
    def user_count(self):
        """Returns number of users in this room"""

        return self._user_count

    @property
    def files(self):
        """Returns list of File objects for this room.
        Note: This will only reflect the files at the time
        this method was called."""

        for fid in self._files.keys():
            if self._files[fid].expired:
                del self._files[fid]
        return list(self._files.values())

    @property
    def filedict(self):
        """Returns dict of File objects for this room.
        Note: This will only reflect the files at the time
        this method was called."""

        for fid in self._files.keys():
            if self._files[fid].expired:
                del self._files[fid]
        return dict(self._files)

    def get_user_stats(self, name):
        """Return data about the given user. Returns None if user
        does not exist."""

        req = self.conn.get(BASE_URL + "/user/" + name)
        if req.status_code != 200 or not name:
            return None

        return json.loads(self.conn.get(BASE_REST_URL + "getUserInfo",
                                        params={"name": name}).text)

    def post_chat(self, msg, is_me=False):
        """Posts a msg to this room's chat. Set me=True if you want to /me"""

        if len(msg) > self._config['max_message']:
            raise ValueError(
                "Chat message must be at most {} characters".format(
                    self._config['max_message']))

        while not self.user.name:
            with ARBITRATOR.condition:
                ARBITRATOR.condition.wait()
        if is_me:
            self.conn.make_call("command", [self.user.name, "me", msg])
            return

        self.conn.make_call("chat", [self.user.name, msg])

    def upload_file(self, filename, upload_as=None, blocksize=None,
                    callback=None):
        """
        Uploads a file with given filename to this room.
        You may specify upload_as to change the name it is uploaded as.
        You can also specify a blocksize and a callback if you wish.
        Returns the file's id on success and None on failure."""

        file = filename if hasattr(filename, "read") else open(filename, 'rb')
        filename = upload_as or os.path.split(filename)[1]
        try:
            file.seek(0, 2)
            if file.tell() > self._config['max_file']:
                raise ValueError(
                    "File must be at most {} GB".format(
                        self._config['max_file'] >> 30))
        finally:
            try:
                file.seek(0)
            except Exception:
                pass

        files = Data({'file': {"name": filename, "value": file}},
                     blocksize=blocksize,
                     callback=callback)

        headers = {'Origin': 'https://volafile.io'}
        headers.update(files.headers)

        key, server, file_id = self._generate_upload_key()
        params = {'room': self.name,
                  'key': key,
                  'filename': filename}

        post = self.conn.post("https://{}/upload".format(server),
                              params=params,
                              data=files,
                              headers=headers)
        if post.status_code == 200:
            return file_id
        else:
            return None

    def close(self):
        """Close connection to this room"""

        if self.connected:
            self.conn.close()
        self.clear()
        del self.conn

    def report(self, reason=""):
        """Reports this room to moderators with optional reason."""

        self.conn.make_call("submitReport", [{"reason": reason}])

    @property
    def config(self):
        """Get config data for this room."""

        return dict(self._config)

    @property
    def title(self):
        """Gets the title name of the room (e.g. /g/entoomen)"""

        return self._config['title']

    @title.setter
    def title(self, new_name):
        """Sets the room name (e.g. /g/entoomen)"""

        if not self.owner:
            raise RuntimeError("You must own this room to do that")
        if len(new_name) > self._config['max_title'] or len(new_name) < 1:
            raise ValueError(
                "Room name length must be between 1 and {} characters.".format(
                    self._config['max_title']))
        self.conn.make_call("editInfo", [{"name": new_name}])
        self._config['title'] = new_name

    @property
    def private(self):
        """True if the room is private, False otherwise"""

        return self._config['private']

    @private.setter
    def private(self, value):
        """Sets the room to private if given True, else sets to public"""

        if not self.owner:
            raise RuntimeError("You must own this room to do that")
        self.conn.make_call("editInfo", [{"private": value}])
        self._config['private'] = value

    @property
    def motd(self):
        """Returns the message of the day for this room"""

        return self._config['motd']

    @motd.setter
    def motd(self, motd):
        """Sets the room's MOTD"""

        if not self.owner:
            raise RuntimeError("You must own this room to do that")
        if len(motd) > 1000:
            raise ValueError("Room's MOTD must be at most 1000 characters")
        self.conn.make_call("editInfo", [{"motd": motd}])
        self._config['motd'] = motd

    def clear(self):
        """Clears the cached information, if any"""

        self._files.clear()

    def _generate_upload_key(self):
        """Generates a new upload key"""

        # Wait for server to set username if not set already.
        while not self.user.name:
            with ARBITRATOR.condition:
                ARBITRATOR.condition.wait()
        info = json.loads(self.conn.get(BASE_REST_URL + "getUploadKey",
                                        params={"name": self.user.name,
                                                "room": self.name}).text)
        return info['key'], info['server'], info['file_id']


class ChatMessage:
    """Basically a struct for a chat message. self.msg holds the
    text of the message, files is a list of Files that were
    linked in the message, and rooms are a list of room
    linked in the message. There are also flags for whether the
    user of the message was logged in, a donor, or an admin."""
    # pylint: disable=too-few-public-methods

    def __init__(self, nick, msg, **kw):
        self.nick = nick
        self.msg = msg

        # Optionals
        self.html_msg = kw.get("html_msg", "")
        for key in ("files", "rooms"):
            setattr(self, key, kw.get(key, ()))
        for key in ("logged_in", "donor", "admin"):
            setattr(self, key, kw.get(key, False))

    def __repr__(self):
        return "<Msg({},{})>".format(self.nick, self.msg)


class File:
    """Basically a struct for a file's info on volafile, with an additional
    method to retrieve the file's URL."""

    def __init__(self, conn, file_id, name, **kw):
        # pylint: disable=invalid-name
        self.conn = conn
        self.id = file_id
        self.name = name

        self._additional = dict(kw)
        self._event = Event()

    def __getattr__(self, name):
        if name not in ("type", "size", "expire_time", "uploader", "info"):
            raise AttributeError("Not a valid key: {}".format(name))
        result = self._additional.get(name, None)
        if result is None:
            self.conn.queues_enabled = False
            try:
                self.conn.make_call("get_fileinfo", [self.id])
                self._event.wait()
                result = self._additional.get(name, None)
            finally:
                self.conn.queues_enabled = True
        return result

    @property
    def url(self):
        """Gets the download url of the file"""

        return "{}/get/{}/{}".format(BASE_URL, self.id, self.name)

    @property
    def expired(self):
        """Returns true if the file has expired, false otherwise"""

        return time.time() >= self.expire_time

    @property
    def time_left(self):
        """Returns how many seconds before this file expires"""

        return self.expire_time - time.time()

    @property
    def thumbnail(self):
        """Returns the thumbnail url for this image, audio, or video file."""

        if self.type not in ("video", "image", "audio"):
            raise RuntimeError("Only videos, audio and images have thumbnails")
        vid = "video_" if self.type == "video" else ""
        return "{}/asset/{}/{}thumb".format(BASE_URL, self.id, vid)

    @property
    def resolution(self):
        """Gets the resolution of this image or video file in format (W, H)"""

        if self.type not in ("video", "image"):
            raise RuntimeError("Only videos and images have resolutions")
        return (self.info['width'], self.info['height'])

    @property
    def duration(self):
        """Returns the duration in seconds of this audio or video file"""

        if self.type not in ("video", "audio"):
            raise RuntimeError("Only videos and audio have durations")
        return self.info.get('length') or self.info.get('duration')

    def __repr__(self):
        return ("<File({},{},{},{})>".
                format(self.id, self.size, self.uploader, self.name))

    def add_info(self, info):
        """Adds info to the file."""

        self.name = info['name']
        add = self._additional
        add["type"] = "other"
        for file_type in ('book', 'image', 'video', 'audio', 'archive'):
            if file_type in info:
                add["type"] = file_type
        add["info"] = info.get(self.type, {})
        add["size"] = info['size']
        add["expire_time"] = info['expires'] / 1000
        add["uploader"] = info['user']
        self._event.set()


class User:
    """Used by Room. Currently not very useful by itself"""

    def __init__(self, name, conn, max_len):
        self._max_length = max_len
        if name is None:
            self.name = ""
        else:
            self._verify_username(name)
        self.name = name
        self.conn = conn
        self.logged_in = False

    def login(self, password):
        """Attempts to log in as the current user with given password"""

        if self.logged_in:
            raise RuntimeError("User already logged in!")

        params = {"name": self.name,
                  "password": password}
        json_resp = json.loads(self.conn.get(BASE_REST_URL + "login",
                                             params=params).text)
        if 'error' in json_resp:
            raise ValueError("Login unsuccessful: {}".
                             format(json_resp["error"]))
        self.conn.make_call("useSession", [json_resp["session"]])
        self.conn.cookies.update({"session": json_resp["session"]})
        self.logged_in = True

    def logout(self):
        """Logs your user out"""

        if not self.logged_in:
            raise RuntimeError("User is not logged in")
        self.conn.make_call("logout", [])
        self.logged_in = False

    def change_nick(self, new_nick):
        """Change the name of your user
        Note: Must be logged out to change nick"""

        if self.logged_in:
            raise RuntimeError("User must be logged out")
        self._verify_username(new_nick)

        self.conn.make_call("command", [self.name, "nick", new_nick])
        self.name = new_nick

    def register(self, password):
        """Registers the current user with the given password."""

        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")

        params = {"name": self.name, "password": password}
        json_resp = json.loads(self.conn.get(BASE_REST_URL + "register",
                                             params=params).text)

        if 'error' in json_resp:
            raise ValueError("User '{}' is already registered".
                             format(self.name))

        self.conn.make_call("useSession", [json_resp["session"]])
        self.conn.cookies.update({"session": json_resp["session"]})
        self.logged_in = True

    def change_password(self, old_pass, new_pass):
        """Changes the password for the currently logged in user."""

        if len(new_pass) < 8:
            raise ValueError("Password must be at least 8 characters.")

        params = {"name": self.name,
                  "password": new_pass,
                  "old_password": old_pass
                  }
        json_resp = json.loads(self.conn.get(BASE_REST_URL + "changePassword",
                                             params=params).text)

        if 'error' in json_resp:
            raise ValueError("Wrong password.")

    def _verify_username(self, username):
        """Raises an exception if the given username is not valid."""

        if len(username) > self._max_length or len(username) < 3:
            raise ValueError(
                "Username must be between 3 and {} characters.".format(
                    self._max_length))
        if any(
                c not in string.ascii_letters +
                string.digits for c in username):
            raise ValueError(
                "Usernames can only contain alphanumeric characters.")

    def __repr__(self):
        return "<User({}, {})>".format(self.name, self.logged_in)


def listen_many(*rooms):
    """Listen for changes in all registered listeners in all specified rooms"""

    rooms = set(r.conn for r in rooms)
    for room in rooms:
        room.validate_listeners()
    with ARBITRATOR.condition:
        while any(r.connected for r in rooms):
            ARBITRATOR.condition.wait()
            rooms = [r for r in rooms if r.run_queues()]
            if not rooms:
                return
