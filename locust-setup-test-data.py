#!/usr/bin/env python3

from gevent import monkey

monkey.patch_all()

import argparse
import csv
import json
import logging
import requests
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import gevent
from gevent import Timeout

from nio.api import RoomVisibility
from nio.responses import (LoginError, LoginResponse, RoomCreateError, RoomSendError)

from matrix_locust.nio.locust_client import LocustClient

import os

log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())
logging.basicConfig(
    level=log_level, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

LOREM_IPSUM = """Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."""

LOREM_WORDS = LOREM_IPSUM.split()

COMMON_REACTIONS = ["👍", "❤️", "😊", "🎉", "🔥", "💯", "😂", "🙌", "✅", "💪"]

MESSAGE_TEMPLATES = [
    "Hey everyone!",
    "Good morning team",
    "Has anyone seen the latest update?",
    "I'll look into that right away",
    "Thanks for sharing!",
    "That makes sense",
    "Let me know if you need any help",
    "Great work on this!",
    "I agree with that approach",
    "What do you think about this?",
    "Can we schedule a quick sync?",
    "I've updated the document",
    "The changes look good to me",
    "I'll have it ready by tomorrow",
    "Quick question about the requirements",
]


class MockResponse:
    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.text = response.text

        try:
            self.js = response.json()
        except (ValueError, json.JSONDecodeError):
            self.js = None

    def success(self):
        pass

    def failure(self, message):
        logger.error(f"Request failed: {message}")


class HostContainer:
    def __init__(self, host):
        self.host = host

    @contextmanager
    def rest(self, method, url, headers=None, json=None, name=None):

        if headers is None:
            headers = {}
        headers.setdefault("Content-Type", "application/json")

        if url.startswith("/"):
            full_url = self.host + url
        else:
            full_url = url

        try:
            response = requests.request(
                method=method, url=full_url, headers=headers, json=json
            )

            mock_resp = MockResponse(response)
            yield mock_resp

        except Exception as e:
            logger.error(f"Request exception: {e}")
            raise


class TestDataGenerator:
    def __init__(self, homeserver: str, setup_users: List[Dict[str, str]], external_users_csv_file: str = "user_external_ids.csv"):
        self.homeserver = homeserver
        self.setup_users = setup_users
        self.external_users_csv_file = external_users_csv_file
        self.clients: Dict[str, LocustClient] = {}
        self.created_rooms: List[Tuple[str, str, str]] = []

    @contextmanager
    def timeout_context(self, seconds: int, error_message: str):
        timeout = Timeout(seconds, TimeoutError(error_message))
        timeout.start()
        try:
            yield
        finally:
            timeout.cancel()

    def check_homeserver_connectivity(self) -> bool:
        logger.info(f"Checking connectivity to homeserver: {self.homeserver}")

        endpoints_to_check = [
            ("/_matrix/client/versions", "Matrix versions endpoint"),
            ("/_matrix/client/v3/login", "Matrix login endpoint"),
            ("/.well-known/matrix/client", "Matrix well-known endpoint")
        ]

        for endpoint, description in endpoints_to_check:
            url = f"{self.homeserver}{endpoint}"
            try:
                logger.info(f"Testing {description}: {url}")
                response = requests.get(url, timeout=15)

                if response.status_code in [200, 404]:  # 404 is acceptable for some endpoints
                    logger.info(f"✓ {description} responded (HTTP {response.status_code})")
                elif response.status_code >= 500:
                    logger.error(f"✗ {description} server error (HTTP {response.status_code})")
                    return False
                else:
                    logger.warning(f"⚠ {description} returned HTTP {response.status_code}")

            except requests.exceptions.Timeout:
                logger.error(f"✗ Timeout connecting to {description}")
                return False
            except requests.exceptions.ConnectionError as e:
                logger.error(f"✗ Connection failed to {description}: {e}")
                return False
            except Exception as e:
                logger.error(f"✗ Error checking {description}: {e}")
                return False

        return True

    def validate_oidc_endpoints(self, oidc_issuer: str, username: str, idx: int, total_users: int) -> bool:
        logger.info(f"[{idx+1}/{total_users}] Validating OIDC endpoints for {username}...")

        try:
            logger.info(f"[{idx+1}/{total_users}] Checking OIDC issuer: {oidc_issuer}")

            well_known_url = f"{oidc_issuer}/.well-known/openid_configuration"
            response = requests.get(well_known_url, timeout=15)

            if response.status_code == 200:
                logger.info(f"[{idx+1}/{total_users}] ✓ OIDC configuration available")
            elif response.status_code >= 500:
                logger.error(f"[{idx+1}/{total_users}] ✗ OIDC issuer server error (HTTP {response.status_code})")
                return False
            else:
                logger.warning(f"[{idx+1}/{total_users}] ⚠ OIDC configuration returned HTTP {response.status_code}")

            sso_redirect_url = f"{self.homeserver}/_matrix/client/v3/login/sso/redirect/oidc-nitroid"
            logger.info(f"[{idx+1}/{total_users}] Testing SSO redirect endpoint: {sso_redirect_url}")

            response = requests.get(sso_redirect_url, timeout=15, allow_redirects=False)

            if response.status_code in [302, 303, 307, 308]:
                logger.info(f"[{idx+1}/{total_users}] ✓ SSO redirect endpoint is working (HTTP {response.status_code})")
                return True
            elif response.status_code >= 500:
                logger.error(f"[{idx+1}/{total_users}] ✗ SSO redirect endpoint server error (HTTP {response.status_code})")
                return False
            else:
                logger.warning(f"[{idx+1}/{total_users}] ⚠ SSO redirect returned unexpected HTTP {response.status_code}")
                return True

        except requests.exceptions.Timeout:
            logger.error(f"[{idx+1}/{total_users}] ✗ Timeout validating OIDC endpoints")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"[{idx+1}/{total_users}] ✗ Connection error validating OIDC: {e}")
            return False
        except Exception as e:
            logger.error(f"[{idx+1}/{total_users}] ✗ Error validating OIDC endpoints: {e}")
            return False

    def login_setup_users(self):
        total_users = len(self.setup_users)
        logger.info(f"Authenticating {total_users} setup users...")

        # Check homeserver connectivity first
        if not self.check_homeserver_connectivity():
            logger.error("Cannot proceed with authentication - homeserver is not reachable")
            return

        for idx, user_data in enumerate(self.setup_users):
            username = user_data["username"]
            password = user_data.get("password")
            oidc_issuer = user_data.get("oidc_issuer")
            oidc_client_id = user_data.get("oidc_client_id", "matrix-locust")

            logger.info(f"[{idx+1}/{total_users}] Starting OIDC authentication for {username}...")

            if not (oidc_issuer and password):
                logger.error(f"User {username} missing OIDC credentials")
                continue

            if not self.validate_oidc_endpoints(oidc_issuer, username, idx, total_users):
                logger.error(f"[{idx+1}/{total_users}] ✗ Skipping {username} due to OIDC validation failure")
                continue

            host_container = HostContainer(self.homeserver)
            device_id = f"SETUP_{uuid.uuid4().hex[:8]}"
            client = LocustClient(
                locust_user=host_container,
                user=username,
                device_id=device_id,
            )

            try:
                logger.info(f"[{idx+1}/{total_users}] Connecting to OIDC issuer: {oidc_issuer}")
                logger.info(f"[{idx+1}/{total_users}] Starting SSO flow for {username}...")

                timeout_msg = f"OIDC login for {username} timed out after 120 seconds"
                with self.timeout_context(120, timeout_msg):
                    response = client.login_oidc(
                        oidc_issuer=oidc_issuer,
                        client_id=oidc_client_id,
                        username=username,
                        password=password,
                    )

                if isinstance(response, LoginResponse):
                    logger.info(f"[{idx+1}/{total_users}] ✓ {username} authenticated successfully (ID: {response.user_id})")
                    self.clients[username] = client
                elif isinstance(response, LoginError):
                    logger.error(f"[{idx+1}/{total_users}] ✗ Authentication failed for {username}: {response.message}")
                else:
                    logger.error(f"[{idx+1}/{total_users}] ✗ Unexpected response for {username}: {type(response)}")

            except TimeoutError as e:
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.error(f"[{idx+1}/{total_users}] ✗ LOGIN TIMEOUT at {current_time}")
                logger.error(f"[{idx+1}/{total_users}] ✗ {username} login timed out after 120 seconds")
                time.sleep(2)
            except Exception as e:
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                    logger.error(f"[{idx+1}/{total_users}] ✗ {username} connection timed out: {e}")
                elif "connection" in str(e).lower() or "refused" in str(e).lower():
                    logger.error(f"[{idx+1}/{total_users}] ✗ {username} connection error: {e}")
                elif "name or service not known" in str(e).lower() or "nodename" in str(e).lower():
                    logger.error(f"[{idx+1}/{total_users}] ✗ Cannot resolve hostname: {self.homeserver}")
                else:
                    logger.exception(f"[{idx+1}/{total_users}] ✗ {username} login failed: {e}")

                time.sleep(1)

        success_count = len(self.clients)
        if success_count > 0:
            logger.info(f"Successfully authenticated {success_count}/{total_users} setup users")
        else:
            logger.error("AUTHENTICATION FAILED - NO USERS LOGGED IN")

    def create_rooms(self, rooms_per_user: int):
        total_rooms = len(self.clients) * rooms_per_user
        logger.info(f"Creating {total_rooms} test rooms ({rooms_per_user} per user)")

        timestamp = datetime.now().strftime("%m%d_%H%M")
        failures = 0

        for username, client in self.clients.items():
            logger.debug(f"Creating rooms for user {username}")
            created_count = 0

            for i in range(rooms_per_user):
                room_name = f"{timestamp} #{i + 1}"

                try:
                    response = client.room_create(
                        name=room_name, visibility=RoomVisibility.public
                    )

                    if isinstance(response, RoomCreateError):
                        logger.debug(f"Room creation failed for {username}: {response.message}")
                        failures += 1
                    elif hasattr(response, "room_id"):
                        room_id = response.room_id
                        self.created_rooms.append((room_id, room_name, username))
                        created_count += 1
                        if created_count % 5 == 0:
                            logger.debug(f"Created {created_count} rooms for {username}")
                    else:
                        logger.debug(f"Unexpected room creation response for {username}: {type(response)}")
                        failures += 1

                except Exception as e:
                    logger.exception(f"Room creation exception for {username}: {e}")
                    failures += 1

                time.sleep(0.1)

            logger.debug(f"Completed room creation for {username}: {created_count}/{rooms_per_user}")

        success_count = len(self.created_rooms)
        logger.info(f"Created {success_count}/{total_rooms} rooms successfully" +
                   (f" ({failures} failures)" if failures > 0 else ""))

    def add_users_via_audiences_api(self):
        if not self.created_rooms:
            logger.info("No rooms to add users to")
            return

        csv_users = []
        try:
            with open(
                self.external_users_csv_file, "r"
            ) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    csv_users.append(
                        {"externalId": row["external_id"], "id": row["external_id"]}
                    )
            logger.info(f"Loaded {len(csv_users)} users from user_external_ids.csv")
        except Exception as e:
            logger.error(f"Failed to load users from CSV: {e}")
            return

        logger.info(
            f"Adding {len(csv_users)} users to {len(self.created_rooms)} rooms via Audiences API..."
        )

        successful_rooms = 0
        failed_rooms = 0

        for room_idx, (room_id, room_name, owner) in enumerate(self.created_rooms):
            logger.debug(f"[{room_idx+1}/{len(self.created_rooms)}] Adding {len(csv_users)} users to {room_name}")

            client = self.clients[owner]

            try:
                room_context_url = "/audiences/api/rooms"
                room_context_payload = {"room": {"mxid": room_id}}

                with client.locust_user.rest(
                    "POST",
                    room_context_url,
                    headers={"Authorization": f"Bearer {client.access_token}"},
                    json=room_context_payload,
                ) as resp:
                    if resp.status_code != 200:
                        logger.error(f"Failed to get room context for {room_name}: HTTP {resp.status_code}")
                        failed_rooms += 1
                        continue

                    if not resp.js or "audiences_key" not in resp.js:
                        logger.error(f"No audiences key in response for {room_name}")
                        failed_rooms += 1
                        continue

                    audiences_key = resp.js["audiences_key"]

                    existing_extra_users = resp.js.get("context", {}).get(
                        "extra_users", []
                    )
                    existing_external_ids = {
                        user.get("externalId") for user in existing_extra_users
                    }
                    extra_users = existing_extra_users.copy()

                    new_users_added = 0
                    for csv_user in csv_users:
                        if csv_user["externalId"] not in existing_external_ids:
                            extra_users.append(csv_user)
                            new_users_added += 1

                update_payload = {
                    "match_all": False,
                    "criteria": [],
                    "extra_users": extra_users,
                }

                encoded_key = urllib.parse.quote(audiences_key, safe="")
                update_url = f"/audiences/{encoded_key}"

                with client.locust_user.rest(
                    "PUT",
                    update_url,
                    headers={"Authorization": f"Bearer {client.access_token}"},
                    json=update_payload,
                ) as resp:
                    if resp.status_code == 200:
                        total_users_now = len(extra_users)
                        logger.debug(
                            f"Updated extra_users for {room_name}: preserved {len(existing_extra_users)} existing + added {new_users_added} new = {total_users_now} total"
                        )
                        successful_rooms += 1
                    else:
                        logger.error(
                            f"Failed to update extra_users for {room_name}: HTTP {resp.status_code} - {resp.text}"
                        )
                        failed_rooms += 1

            except Exception as e:
                logger.exception(f"Exception adding users to {room_name}: {e}")
                failed_rooms += 1

            time.sleep(0.1)

        logger.info(
            f"✓ Audiences API summary: {successful_rooms}/{len(self.created_rooms)} rooms updated with {len(csv_users)} CSV users, {failed_rooms} failed\n"
        )

    def generate_message_history(
        self, messages_per_room: int = 10, reactions_per_room: int = 0
    ):
        total_rooms = len(self.created_rooms)
        total_messages = total_rooms * messages_per_room
        logger.info(
            f"Generating message history: {total_messages} total messages across {total_rooms} rooms"
        )

        start_time = time.time()

        for room_idx, (room_id, room_name, owner) in enumerate(self.created_rooms):
            if room_idx % 10 == 0 or room_idx < 5:
                logger.debug(f"[{room_idx+1}/{total_rooms}] {owner} messaging in {room_name}")

            client = self.clients[owner]
            message_events = []
            messages_sent = 0

            base_time = datetime.now() - timedelta(days=7)

            for i in range(messages_per_room):
                time_offset = timedelta(
                    days=i % 7, hours=(i * 3) % 24, minutes=(i * 5) % 60
                )
                message_time = base_time + time_offset

                message_content = self._generate_message(i)

                content = {
                    "msgtype": "m.text",
                    "body": message_content,
                    "timestamp": int(message_time.timestamp() * 1000),
                }

                response = client.room_send(
                    room_id=room_id, message_type="m.room.message", content=content
                )

                if isinstance(response, RoomSendError):
                    logger.debug(f"Message send failed for {owner} in {room_name}: {response.message if hasattr(response, 'message') else 'Unknown error'}")
                else:
                    message_events.append((response.event_id, client))
                    messages_sent += 1

                    if i % 5 == 2 and len(message_events) > 1:
                        reply_to = message_events[max(0, len(message_events) - 3)]
                        self._send_reply(client, room_id, reply_to[0], i)

                time.sleep(0.02)

            if room_idx % 10 == 0 or room_idx < 5:
                logger.debug(f"Completed messaging for {owner} in {room_name}: {messages_sent}/{messages_per_room}")

            if reactions_per_room > 0 and len(message_events) >= reactions_per_room:
                self._add_reactions(room_id, message_events, owner, reactions_per_room)

        total_elapsed = time.time() - start_time
        logger.info(f"✓ Message generation complete: {total_elapsed:.1f}s total\n")

    def _send_reply(
        self, client: LocustClient, room_id: str, reply_to_event_id: str, index: int
    ):
        replies = [
            "I agree with this",
            "Good point!",
            "Let me check on that",
            "Makes sense to me",
            "Thanks for clarifying",
        ]
        reply_content = {
            "msgtype": "m.text",
            "body": replies[index % len(replies)],
            "m.relates_to": {"m.in_reply_to": {"event_id": reply_to_event_id}},
        }

        client.room_send(
            room_id=room_id, message_type="m.room.message", content=reply_content
        )

    def _add_reactions(
        self,
        room_id: str,
        message_events: List[Tuple[str, LocustClient]],
        owner: str,
        reactions_per_room: int,
    ):
        reaction_count = min(reactions_per_room, len(message_events))

        reactions_added = 0
        for i in range(reaction_count):
            event_id, _ = message_events[i * 3 % len(message_events)]
            reactor = self.clients[owner]
            reaction = COMMON_REACTIONS[i % len(COMMON_REACTIONS)]

            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": reaction,
                }
            }

            response = reactor.room_send(
                room_id=room_id, message_type="m.reaction", content=content
            )

            if isinstance(response, RoomSendError):
                logger.warning(f"    Failed to add reaction: {response.message}")
            else:
                reactions_added += 1

            time.sleep(0.01)

        logger.info(f"    Added {reactions_added} reactions")

    def add_read_receipts(self):
        total_rooms = len(self.created_rooms)
        logger.info(f"Adding read receipts for {total_rooms} rooms...")

        receipts_added = 0
        for room_idx, (room_id, room_name, owner) in enumerate(self.created_rooms):
            logger.debug(f"  [{room_idx+1}/{total_rooms}] Processing {room_name}")

            client = self.clients[owner]
            messages_response = client.room_messages(
                room_id=room_id, start="", limit=10
            )

            if hasattr(messages_response, "chunk") and messages_response.chunk:
                target_event = messages_response.chunk[
                    len(messages_response.chunk) // 2
                ]
                if hasattr(target_event, "event_id"):
                    client.update_receipt_marker(
                        room_id=room_id, event_id=target_event.event_id
                    )
                    receipts_added += 1

            time.sleep(0.02)

        logger.info(f"✓ Added {receipts_added} read receipts\n")

    def _get_room_size(self, index: int) -> int:
        sizes = [5, 10, 15, 20, 30, 50]
        return sizes[index % len(sizes)]

    def _ensure_user_in_room(self, client: LocustClient, room_id: str):
        try:
            join_response = client.join(room_id)
            if hasattr(join_response, "room_id"):
                pass
        except Exception as e:
            logger.warning(f"Failed to join room {room_id}: {e}")

    def _generate_message(self, index: int) -> str:
        message_type = index % 10

        if message_type < 3:
            return MESSAGE_TEMPLATES[index % len(MESSAGE_TEMPLATES)]
        elif message_type < 7:
            word_count = 5 + (index % 20)
            start_idx = (index * 3) % max(1, len(LOREM_WORDS) - word_count)
            return " ".join(LOREM_WORDS[start_idx : start_idx + word_count])
        else:
            sentences = 1 + (index % 3)
            message = []
            for j in range(sentences):
                word_count = 5 + ((index + j) % 10)
                start_idx = ((index + j) * 5) % max(1, len(LOREM_WORDS) - word_count)
                sentence = " ".join(LOREM_WORDS[start_idx : start_idx + word_count])
                message.append(sentence.capitalize() + ".")
            return " ".join(message)

    def cleanup(self):
        for client in self.clients.values():
            client.logout()


def main(
    host=None,
    setup_users_file="setup-users.csv",
    test_users_file="users.csv",
    messages_per_room=10,
    room_count=0,
    reactions_per_room=0,
    external_users_csv_file="user_external_ids.csv",
):
    logger.info("=" * 70)
    logger.info("MATRIX LOAD TEST - DATA SETUP (OIDC)")
    logger.info("=" * 70)

    rooms_per_user = 1
    all_users = []

    if setup_users_file == test_users_file:
        with open(setup_users_file, "r") as f:
            reader = csv.DictReader(f)
            all_users = list(reader)
        setup_users = all_users[:3]
        test_users = all_users[3:]
    else:
        with open(setup_users_file, "r") as f:
            reader = csv.DictReader(f)
            setup_users = list(reader)

        with open(test_users_file, "r") as f:
            reader = csv.DictReader(f)
            test_users = list(reader)

        all_users = setup_users + test_users

    if host:
        homeserver = host
        host_domain = host.replace("https://", "").replace("http://", "").split(":")[0]
    else:
        homeserver = "http://localhost:8008"
        if setup_users and "homeserver" in setup_users[0]:
            homeserver = setup_users[0]["homeserver"]
            if not homeserver.startswith("http"):
                homeserver = f"http://{homeserver}"

    logger.info(f"Configuration:")
    logger.info(f"  Homeserver: {homeserver}")
    logger.info(f"  Setup users: {len(setup_users)} (from {setup_users_file})")
    logger.info(f"  Test users: {len(test_users)} (from {test_users_file})")
    logger.info("")

    generator = TestDataGenerator(homeserver, setup_users, external_users_csv_file)

    try:
        logger.info("=" * 50)
        logger.info("STEP 1: Authentication (Setup Users Only)")
        logger.info("=" * 50)
        generator.login_setup_users()

        if not generator.clients:
            logger.error("No setup users were successfully authenticated. Exiting.")
            logger.error("Please check:")
            logger.error("  - OIDC issuer and client_id in your CSV")
            logger.error("  - Username and password are correct")
            logger.error("  - The homeserver URL is accessible")
            return

        if room_count > 0:
            rooms_per_user = max(1, room_count // len(generator.clients))

            logger.info("=" * 50)
            logger.info(
                f"STEP 2: Room Creation ({rooms_per_user} rooms per setup user, {room_count} total)"
            )
            logger.info("=" * 50)
            generator.create_rooms(rooms_per_user)

            logger.info("=" * 50)
            logger.info("STEP 3: Invite Users via Matrix API")
            logger.info("=" * 50)
            generator.add_users_via_audiences_api()
        else:
            logger.info("=" * 50)
            logger.info("STEP 2: SKIPPED - No rooms requested (--rooms=0)")
            logger.info("=" * 50)

        logger.info("=" * 50)
        logger.info("STEP 4: Message Generation")
        logger.info("=" * 50)
        generator.generate_message_history(
            messages_per_room=messages_per_room, reactions_per_room=reactions_per_room
        )

        logger.info("=" * 50)
        logger.info("STEP 5: Read Receipts")
        logger.info("=" * 50)
        generator.add_read_receipts()

        with open("test_rooms.json", "w") as f:
            json.dump(
                [
                    {"room_id": rid, "name": name, "owner": owner}
                    for rid, name, owner in generator.created_rooms
                ],
                f,
                indent=2,
            )

        logger.info("=" * 70)
        logger.info("✓ TEST DATA SETUP COMPLETE!")
        logger.info("=" * 70)
        logger.info(f"Summary:")
        logger.info(f"  • Setup users authenticated: {len(generator.clients)}")
        if room_count > 0:
            logger.info(
                f"  • Created rooms: {len(generator.created_rooms)} ({rooms_per_user} per setup user)"
            )
            logger.info(
                f"  • Invited users via Matrix API: {len(all_users)} users to all rooms"
            )
        else:
            logger.info(f"  • No new rooms created (--rooms=0)")
        logger.info(
            f"  • Generated messages: ~{len(generator.created_rooms) * messages_per_room} (from room owners)"
        )
        if reactions_per_room > 0:
            logger.info(
                f"  • Added reactions: ~{len(generator.created_rooms) * reactions_per_room}"
            )
        else:
            logger.info(f"  • No reactions added (--reactions=0)")
        logger.info(f"  • Added read receipts")
        logger.info(f"  • Saved room list to: test_rooms.json")
        logger.info("=" * 70)

    finally:
        generator.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Setup test data for Matrix load testing (OIDC version)"
    )
    parser.add_argument(
        "--host",
        type=str,
        help="Matrix homeserver URL (e.g., http://localhost:8008 or https://matrix.example.com)",
    )
    parser.add_argument(
        "--setup-users",
        type=str,
        default="users.csv",
        help="CSV file containing setup users who will create rooms/messages (default: users.csv)",
    )
    parser.add_argument(
        "--test-users",
        type=str,
        default="users.csv",
        help="CSV file containing test users who will be added to rooms (default: users.csv)",
    )
    parser.add_argument(
        "--messages",
        type=int,
        default=10,
        help="Number of messages per room (default: 10)",
    )
    parser.add_argument(
        "--rooms",
        type=int,
        default=0,
        help="Total number of rooms to create (default: 0, no rooms)",
    )
    parser.add_argument(
        "--reactions",
        type=int,
        default=0,
        help="Number of reactions to add per room (default: 0, no reactions)",
    )
    parser.add_argument(
        "--external-users-csv",
        type=str,
        default="user_external_ids.csv",
        help="CSV file containing external user IDs to add via Audiences API (default: user_external_ids.csv)",
    )

    args = parser.parse_args()
    main(
        host=args.host,
        setup_users_file=args.setup_users,
        test_users_file=args.test_users,
        messages_per_room=args.messages,
        room_count=args.rooms,
        reactions_per_room=args.reactions,
        external_users_csv_file=args.external_users_csv,
    )
