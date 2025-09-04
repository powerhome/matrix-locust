###########################################################
#
# matrixchatuser.py - The MatrixChatUser class
# -- Acts like a Matrix chat user
#
# Created: 2022-08-05
# Author: Charles V Wright <cvwright@futo.org>
# Copyright: 2022 FUTO Holdings Inc
# License: Apache License version 2.0
#
# The MatrixChatUser class extends MatrixUser to add some
# basic chatroom user behaviors.

# Upon login to the homeserver, this user spawns a second
# "background" Greenlet to act as the user's client's
# background sync task.  The "background" Greenlet sleeps and
# calls /sync in an infinite loop, and it uses the responses
# to /sync to populate the user's local understanding of the
# world state.
#
# Meanwhile, the user's main "foreground" Greenlet does the
# things that a Locust User normally does, sleeping and then
# picking a random @task to execute.  The available set of
# @tasks includes: accepting invites to join rooms, sending
# m.text messages, sending reactions, and paginating backward
# in a room.
#
###########################################################

import csv
import glob
import json
import logging
import os
import random
import resource
import sys
import uuid
from typing import Optional
from contextlib import contextmanager

import gevent
from locust import TaskSet, between, events, task
from locust.runners import MasterRunner, WorkerRunner
from nio import MatrixRoom, RoomMessageText
from nio.api import _FilterT
from nio.responses import (LoginError, ProfileSetDisplayNameError,
                           RoomMessagesError, RoomSendError, SyncError, LoginResponse)

from matrix_locust.users.matrixuser import MatrixUser
from matrix_locust.nio.locust_client import LocustClient

# Preflight ###############################################


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    # Increase resource limits to prevent OS running out of descriptors
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    # Multi-worker
    if isinstance(environment.runner, WorkerRunner):
        print(f"Registered 'load_users' handler on {environment.runner.client_id}")
        environment.runner.register_message("load_users", MatrixChatUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(
        environment.runner, MasterRunner
    ):
        # Open our list of users
        MatrixChatUser.worker_users = csv.DictReader(open("users.csv"))


# Load our images and thumbnails
images_folder = "images"
image_files = glob.glob(os.path.join(images_folder, "*.jpg"))
images_with_thumbnails = []
delay_multiplier = 0.1
for image_filename in image_files:
    image_basename = os.path.basename(image_filename)
    thumbnail_filename = os.path.join(images_folder, "thumbnails", image_basename)
    if os.path.exists(thumbnail_filename):
        images_with_thumbnails.append(image_filename)

# Find our user avatar images
avatars = []
avatars_folder = "avatars"
avatar_files = glob.glob(os.path.join(avatars_folder, "*.png"))

# Pre-generate some messages for the users to send
lorem_ipsum_text = """
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.
"""
lorem_ipsum_words = lorem_ipsum_text.split()

lorem_ipsum_messages = {}
for i in range(1, len(lorem_ipsum_words) + 1):
    lorem_ipsum_messages[i] = " ".join(lorem_ipsum_words[:i])

###########################################################

class MatrixChatUser(MatrixUser):
    worker_id = None
    worker_users = []

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixChatUser.worker_users = iter(msg.data)
        MatrixChatUser.worker_id = environment.runner.client_id
        logging.info(
            "Worker [%s] Received %s users", environment.runner.client_id, len(msg.data)
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.room_messages = {}
        self.recent_messages = {}
        self.earliest_sync_tokens = {}
        self.user_avatar_urls = {}
        self.user_display_names = {}
        self.matrix_sync_task = None
        self.initial_sync_token = None

        self.matrix_client.add_event_callback(self.message_callback, RoomMessageText)

    def on_start(self):
        # Load the next user who needs to be logged-in
        try:
            user = next(MatrixChatUser.worker_users)
        except StopIteration:
            gevent.sleep(999999)
            return

        # Change to force user login request and refresh tokens
        invalidate_access_tokens = False

        self.login_from_csv_real_oidc(user)

        if self.matrix_client.user is None or self.matrix_client.password is None:
            logging.error("Couldn't get username/password. Skipping...")
            return

        if invalidate_access_tokens:
            self.matrix_client.user_id = None
            self.matrix_client.access_token = None

        # Log in as this current user if not already logged in
        if (
            self.matrix_client.user_id is None
            or self.matrix_client.access_token is None
            or len(self.matrix_client.user_id) < 1
            or len(self.matrix_client.access_token) < 1
        ):

            while True:
                # host_container = HostContainer(self.host)
                device_id = f"LOCUSTTEST_{uuid.uuid4().hex[:8]}"
                # self.matrix_client.locust_user=host_container
                self.matrix_client.device_id=device_id

                response = self.matrix_client.login_oidc(
                    oidc_issuer=self.matrix_client.oidc_issuer,
                    client_id=self.matrix_client.oidc_client_id,
                    username=self.matrix_client.user,
                    password=self.matrix_client.password
                )

                if isinstance(response, LoginError):
                    logging.info(response)
                    logging.error("Login failed for User [%s]", self.matrix_client.user)
                    return
                else:
                    break

        # Spawn a Greenlet to act as this user's client, constantly /sync'ing with the server
        self.matrix_sync_task = gevent.spawn(
            self.sync_forever, client_sleep=None, timeout=30_000
        )

        # Wait a bit before we take our first action
        self.wait()

    def on_stop(self):
        pass
        # Currently we don't want to invalidate access tokens stored in the csv file
        # self.logout()

    def get_random_roomid(self):
        if len(self.matrix_client.rooms) > 0:
            room_id = random.choice(list(self.matrix_client.rooms.keys()))
            return room_id
        else:
            return None

    def load_data_for_room(self, room_id):
        # FIXME Need to parse the room state for all of this :-\
        ## FIXME Load the room displayname and avatar url
        ## FIXME If we don't have it, load the avatar image
        # room_displayname = self.room_display_names.get(room_id, None)
        # if room_displayname is None:
        #  # Uh-oh, do we need to parse the room state from /sync in order to get this???
        #  pass
        # room_avatar_url = self.room_avatar_urls.get(room_id, None)
        # if room_avatar_url is None:
        #  # Uh-oh, do we need to parse the room state from /sync in order to get this???
        #  pass
        ## Note: We may have just set room_avatar_url in the code above
        # if room_avatar_url is not None and self.media_cache.get(room_avatar_url, False) is False:
        #  # FIXME Download the image and set the cache to True
        #  pass

        # Load the avatars for recent users
        # Load the thumbnails for any messages that have one
        messages = self.recent_messages.get(room_id, [])
        for message in messages:
            sender_userid = message.sender
            sender_avatar_mxc = self.user_avatar_urls.get(sender_userid, None)
            if sender_avatar_mxc is None:
                # FIXME Fetch the avatar URL for sender_userid
                # FIXME Set avatar_mxc
                # FIXME Set self.user_avatar_urls[sender_userid]
                self.matrix_client.get_avatar(sender_userid)
            # Try again.  Maybe we were able to populate the cache in the line above.
            sender_avatar_mxc = self.user_avatar_urls.get(sender_userid, None)
            # Now avatar_mxc might not be None, even if it was above
            if sender_avatar_mxc is not None and len(sender_avatar_mxc) > 0:
                # FIXME Reimplement method with nio after avatar support is added
                self.download_matrix_media(sender_avatar_mxc)
            sender_displayname = self.user_display_names.get(sender_userid, None)
            if sender_displayname is None:
                sender_displayname = self.matrix_client.get_displayname(sender_userid)

        # Currently users only send text messages
        # for message in messages:
        #     content = message.content
        #     msgtype = content.msgtype
        #     if msgtype in ["m.image", "m.video", "m.file"]:
        #         thumb_mxc = message.content.get("thumbnail_url", None)
        #         if thumb_mxc is not None:
        #             self.download_matrix_media(thumb_mxc)

    def sync_forever(
        self,
        client_sleep: Optional[float] = None,
        timeout: Optional[int] = None,
        sync_filter: _FilterT = None,
        since: Optional[str] = None,
        full_state: Optional[bool] = None,
        set_presence: Optional[str] = None,
    ):
        # client_sleep is in seconds

        # Continually call the /sync endpoint
        # Put anything that the user might care about into our instance variables where the
        # user @task's can find it
        while True:
            response = self.matrix_client.sync(
                timeout, sync_filter, since, full_state, set_presence
            )

            if isinstance(response, SyncError):
                logging.error(
                    "[%s] /sync error (%s): %s",
                    self.matrix_client.user,
                    response.status_code,
                    response.message,
                )
            else:
                if self.initial_sync_token is None:
                    self.initial_sync_token = response.next_batch

            if not (client_sleep is None):
                gevent.sleep(client_sleep)

    def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        # Add the new messages to whatever we had before (if anything)
        if self.room_messages.get(room.room_id) is None:
            self.room_messages[room.room_id] = []
        self.room_messages[room.room_id].append(event)

        # Store only the most recent 10 messages, regardless of how many we had before or how many we just received
        self.recent_messages[room.room_id] = self.room_messages[room.room_id][-10:]

    @task(5)
    def do_nothing(self):
        self.wait()

    @task(1)
    def send_text(self):
        room_id = self.get_random_roomid()
        if room_id is None:
            logging.warning(
                "User [%s] couldn't get a room for send_text" % self.matrix_client.user
            )
            return
        logging.info(
            "User [%s] sending a message to room [%s]"
            % (self.matrix_client.user, room_id)
        )

        # Send the typing notification like a real client would
        self.matrix_client.room_typing(room_id, True)
        # Sleep while we pretend the user is banging on the keyboard
        delay = random.expovariate(1.0 / 5.0)
        gevent.sleep(delay * delay_multiplier)

        message_len = round(random.lognormvariate(1.0, 1.0))
        message_len = min(message_len, len(lorem_ipsum_words))
        message_len = max(message_len, 1)
        message_text = lorem_ipsum_messages[message_len]
        message_content = {
            "msgtype": "m.text",
            "body": message_text,
        }

        response = self.matrix_client.room_send(
            room_id, "m.room.message", message_content
        )
        if isinstance(response, RoomSendError):
            logging.error(
                "[%s] failed to send m.text to room [%s]",
                self.matrix_client.user,
                room_id,
            )

    @task(4)
    def look_at_room(self):
        room_id = self.get_random_roomid()
        if room_id is None:
            logging.warning(
                "User [%s] couldn't get a roomid for look_at_room"
                % self.matrix_client.user
            )
            return
        logging.info(
            "User [%s] looking at room [%s]" % (self.matrix_client.user, room_id)
        )

        self.load_data_for_room(room_id)

        if len(self.recent_messages.get(room_id, [])) < 1:
            return

        event_id = self.recent_messages[room_id][-1].event_id
        self.matrix_client.update_receipt_marker(room_id, event_id)

    # FIXME Combine look_at_room() and paginate_room() into a TaskSet,
    #       so the user can paginate and scroll the room for a longer
    #       period of time.
    #       In this model, we should load the displaynames and avatars
    #       and message thumbnails every time we paginate, just like a
    #       real client would do as the user scrolls the timeline.
    @task
    def paginate_room(self):
        room_id = self.get_random_roomid()
        token = self.earliest_sync_tokens.get(room_id, self.initial_sync_token)
        if room_id is None or token is None:
            return

        response = self.matrix_client.room_messages(room_id, token)
        if isinstance(response, RoomMessagesError):
            logging.error(
                "[%s] failed /messages failed for room [%s]",
                self.matrix_client.user,
                room_id,
            )
        else:
            self.earliest_sync_tokens[room_id] = response.end

    @task(1)
    def go_afk(self):
        logging.info("[%s] going away from keyboard", self.matrix_client.user)
        # Generate large(ish) random away time
        # away_time = random.expovariate(1.0 / 600.0)  # Expected value = 10 minutes
        away_time = random.expovariate(1.0 / 60.0)  # Expected value = 1 minutes
        gevent.sleep(away_time * delay_multiplier)

    @task(1)
    def change_displayname(self):
        user_number = self.matrix_client.user.split(".")[-1]
        random_number = random.randint(1, 1000)
        new_name = "User %s (random=%d)" % (user_number, random_number)

        response = self.matrix_client.set_displayname(new_name)
        if isinstance(response, ProfileSetDisplayNameError):
            logging.error(
                "[%s] failed to set displayname to %s: Code=%s, Message=%s",
                self.matrix_client.user,
                new_name,
                response.status_code,
                response.message,
            )

    @task(3)
    class ChatInARoom(TaskSet):

        def wait_time(self):
            expected_wait = 25.0
            rate = 1.0 / expected_wait * delay_multiplier
            return random.expovariate(rate)

        def on_start(self):
            # logging.info("User [%s] chatting in a room" % self.user.username)
            if len(self.user.matrix_client.rooms.keys()) == 0:
                self.interrupt()
            else:
                self.room_id = self.user.get_random_roomid()
                self.reacted_messages = []

                if self.room_id is None:
                    self.interrupt()
                else:
                    self.user.load_data_for_room(self.room_id)

        @task
        def send_text(self):
            # Send the typing notification like a real client would
            self.user.matrix_client.room_typing(self.room_id, True)
            # Sleep while we pretend the user is banging on the keyboard
            delay = random.expovariate(1.0 / 5.0)
            gevent.sleep(delay * delay_multiplier)

            message_len = round(random.lognormvariate(1.0, 1.0))
            message_len = min(message_len, len(lorem_ipsum_words))
            message_len = max(message_len, 1)
            message_text = lorem_ipsum_messages[message_len]
            message_content = {
                "msgtype": "m.text",
                "body": message_text,
            }

            response = self.user.matrix_client.room_send(
                self.room_id, "m.room.message", message_content
            )
            if isinstance(response, RoomSendError):
                logging.error(
                    "[%s] failed to send/chat in room [%s]",
                    self.user.matrix_client.user,
                    self.room_id,
                )

        @task
        def send_image(self):
            # Choose an image to send/upload
            # Upload the thumbnail -- FIXME We need to have all of the thumbnails created and stored *before* we start the test.  Performance will be awful if we're trying to dynamically resample the images on-the-fly here in the load generator.
            # Upload the image data, get back an MXC URL
            # Craft the event JSON structure
            # Send the event
            pass

        @task
        def send_reaction(self):
            # Pick a recent message from the selected room,
            # and react to it
            if len(self.user.recent_messages.get(self.room_id, [])) < 1:
                return

            message = random.choice(self.user.recent_messages[self.room_id])
            reaction = random.choice(["💩", "👍", "❤️", "👎", "🤯", "😱", "👏"])
            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": message.event_id,
                    "key": reaction,
                }
            }

            # Prevent errors with reacting to the same message with the same reaction
            if (message, reaction) in self.reacted_messages:
                return
            else:
                self.reacted_messages.append((message, reaction))

            # logging.info("[%s] sending reaction %s to message %s in room %s with event %s",
            #              self.user.matrix_client.user, reaction, message, self.room_id, message.event_id)
            response = self.user.matrix_client.room_send(
                self.room_id, "m.reaction", content
            )
            if isinstance(response, RoomSendError):
                logging.error(
                    "[%s] failed to send reaction in room [%s]: Code=%s, Message=%s",
                    self.user.matrix_client.user,
                    response.room_id,
                    response.status_code,
                    response.message,
                )

        @task
        def stop(self):
            logging.info(
                "User [%s] stopping chat in room [%s]"
                % (self.user.matrix_client.user, self.room_id)
            )
            self.interrupt()

        # Each time we create a new instance of this task, we want to have the user
        # generate a slightly different expected number of messages.
        # FIXME Hmmm this doesn't seem to work...
        tasks = {
            send_text: max(1, round(random.gauss(15, 4))),
            send_image: random.choice([0, 0, 0, 1, 1, 2]),
            send_reaction: random.choice([0, 0, 1, 1, 1, 2, 3]),
            stop: 1,
        }
