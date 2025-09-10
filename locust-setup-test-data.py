#!/usr/bin/env python3

from gevent import monkey

monkey.patch_all()

import argparse
import csv
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Tuple

import gevent
from gevent import Timeout
from nio.api import RoomVisibility
from nio.responses import (LoginError, LoginResponse, RoomCreateError,
                           RoomSendError)

from matrix_locust.nio.locust_client import LocustClient
from matrix_locust.users.matrixuser import MatrixUser

log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class TestDataGenerator:
    def __init__(
        self,
        homeserver: str,
        setup_users: List[Dict[str, str]],
        external_users_csv_file: str = "user_external_ids.csv",
    ):
        self.homeserver = homeserver
        self.setup_users = setup_users
        self.external_users_csv_file = external_users_csv_file
        self.clients: Dict[str, LocustClient] = {}
        self.created_rooms: List[Tuple[str, str, str]] = []

    def login_setup_users(self):
        total_users = len(self.setup_users)
        logger.info(f"Authenticating {total_users} setup users...")

        for idx, user_data in enumerate(self.setup_users):
            username = user_data["username"]
            password = user_data.get("password")
            oidc_issuer = user_data.get("oidc_issuer")
            oidc_client_id = user_data.get("oidc_client_id", "matrix-locust")

            logger.info(
                f"[{idx+1}/{total_users}] Starting OIDC authentication for {username}..."
            )

            if not (oidc_issuer and password):
                logger.error(f"User {username} missing OIDC credentials")
                continue

            class SimpleHost:
                def __init__(self, host):
                    self.host = host

            host_container = SimpleHost(self.homeserver)
            import uuid

            device_id = f"SETUP_{uuid.uuid4().hex[:8]}"

            client = LocustClient(
                locust_user=host_container,
                user=username,
                device_id=device_id,
            )

            try:
                response = client.login_oidc(
                    oidc_issuer=oidc_issuer,
                    client_id=oidc_client_id,
                    username=username,
                    password=password,
                )

                if isinstance(response, LoginResponse):
                    logger.info(
                        f"[{idx+1}/{total_users}] ✓ {username} authenticated successfully"
                    )
                    self.clients[username] = client
                elif isinstance(response, LoginError):
                    logger.error(
                        f"[{idx+1}/{total_users}] ✗ Authentication failed for {username}: {response.message}"
                    )
                else:
                    logger.error(
                        f"[{idx+1}/{total_users}] ✗ Unexpected response for {username}"
                    )

            except Exception as e:
                logger.error(f"[{idx+1}/{total_users}] ✗ {username} login failed: {e}")
                time.sleep(1)

        success_count = len(self.clients)
        if success_count > 0:
            logger.info(
                f"Successfully authenticated {success_count}/{total_users} setup users"
            )
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
                        logger.debug(
                            f"Room creation failed for {username}: {response.message}"
                        )
                        failures += 1
                    elif hasattr(response, "room_id"):
                        room_id = response.room_id
                        self.created_rooms.append((room_id, room_name, username))
                        created_count += 1
                        if created_count % 5 == 0:
                            logger.debug(
                                f"Created {created_count} rooms for {username}"
                            )
                    else:
                        logger.debug(
                            f"Unexpected room creation response for {username}"
                        )
                        failures += 1

                except Exception as e:
                    logger.exception(f"Room creation exception for {username}: {e}")
                    failures += 1

                time.sleep(0.1)

            logger.debug(
                f"Completed room creation for {username}: {created_count}/{rooms_per_user}"
            )

        success_count = len(self.created_rooms)
        logger.info(
            f"Created {success_count}/{total_rooms} rooms successfully"
            + (f" ({failures} failures)" if failures > 0 else "")
        )

    def add_users_via_audiences_api(self):
        if not self.created_rooms:
            logger.info("No rooms to add users to")
            return

        csv_users = []
        try:
            with open(self.external_users_csv_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    csv_users.append(
                        {"externalId": row["external_id"], "id": row["external_id"]}
                    )
            logger.info(
                f"Loaded {len(csv_users)} users from {self.external_users_csv_file}"
            )
        except Exception as e:
            logger.error(f"Failed to load users from CSV: {e}")
            return

        logger.info(
            f"Adding {len(csv_users)} users to {len(self.created_rooms)} rooms via Audiences API..."
        )

        successful_rooms = 0
        failed_rooms = 0

        for room_idx, (room_id, room_name, owner) in enumerate(self.created_rooms):
            logger.debug(
                f"[{room_idx+1}/{len(self.created_rooms)}] Adding {len(csv_users)} users to {room_name}"
            )

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
                        logger.error(
                            f"Failed to get room context for {room_name}: HTTP {resp.status_code}"
                        )
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
                logger.debug(
                    f"[{room_idx+1}/{total_rooms}] {owner} messaging in {room_name}"
                )

            client = self.clients[owner]
            message_events = []
            messages_sent = 0

            MESSAGE_TEMPLATES = [
                "Hey everyone!",
                "Good morning team",
                "Thanks for sharing!",
                "Great work on this!",
                "I agree with that approach",
                "Let me know if you need any help",
            ]

            for i in range(messages_per_room):
                message_content = MESSAGE_TEMPLATES[i % len(MESSAGE_TEMPLATES)]

                content = {
                    "msgtype": "m.text",
                    "body": message_content,
                }

                response = client.room_send(
                    room_id=room_id, message_type="m.room.message", content=content
                )

                if isinstance(response, RoomSendError):
                    logger.debug(f"Message send failed for {owner} in {room_name}")
                else:
                    message_events.append((response.event_id, client))
                    messages_sent += 1

                time.sleep(0.02)

            if room_idx % 10 == 0 or room_idx < 5:
                logger.debug(
                    f"Completed messaging for {owner} in {room_name}: {messages_sent}/{messages_per_room}"
                )

            if reactions_per_room > 0 and len(message_events) >= reactions_per_room:
                self._add_reactions(room_id, message_events, owner, reactions_per_room)

        total_elapsed = time.time() - start_time
        logger.info(f"✓ Message generation complete: {total_elapsed:.1f}s total\n")

    def _add_reactions(
        self, room_id: str, message_events: List, owner: str, reactions_per_room: int
    ):
        client = self.clients[owner]
        reactions = ["👍", "❤️", "😊", "🎉", "🔥"]

        import random

        for i in range(min(reactions_per_room, len(message_events))):
            event_id, _ = message_events[i]
            reaction = random.choice(reactions)

            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": reaction,
                }
            }

            client.room_send(room_id, "m.reaction", content)
            time.sleep(0.01)

    def add_read_receipts(self):
        logger.info("Adding read receipts to messages...")

        for room_id, room_name, owner in self.created_rooms:
            try:
                client = self.clients[owner]
                response = client.room_messages(room_id, limit=1)
                if hasattr(response, "chunk") and response.chunk:
                    latest_event = response.chunk[0]
                    if hasattr(latest_event, "event_id"):
                        client.update_receipt_marker(room_id, latest_event.event_id)
            except Exception as e:
                logger.debug(f"Failed to add read receipt for {room_name}: {e}")

    def cleanup(self):
        logger.debug("Cleaning up test data generator...")
        for username, client in self.clients.items():
            try:
                if hasattr(client, "logout"):
                    client.logout()
            except:
                pass


def main(
    host: str = None,
    setup_users_file: str = "setup-users.csv",
    test_users_file: str = "users.csv",
    messages_per_room: int = 10,
    room_count: int = 0,
    reactions_per_room: int = 0,
    external_users_csv_file: str = "user_external_ids.csv",
):
    if not host:
        logger.error("Please provide a homeserver URL with --host")
        return

    logger.info("=" * 70)
    logger.info("MATRIX LOAD TEST DATA SETUP (Connect v3)")
    logger.info("=" * 70)
    logger.info(f"Homeserver: {host}")
    logger.info(f"Setup users file: {setup_users_file}")
    logger.info(f"Test users file: {test_users_file}")
    logger.info(f"Target rooms to create: {room_count}")
    logger.info(f"Messages per room: {messages_per_room}")
    logger.info(f"Reactions per room: {reactions_per_room}")
    logger.info("=" * 70)

    try:
        with open(setup_users_file, "r") as f:
            reader = csv.DictReader(f)
            setup_users = list(reader)
        logger.info(f"Loaded {len(setup_users)} setup users from {setup_users_file}")
    except Exception as e:
        logger.error(f"Failed to load setup users: {e}")
        return

    try:
        with open(test_users_file, "r") as f:
            reader = csv.DictReader(f)
            all_users = list(reader)
        logger.info(f"Loaded {len(all_users)} test users from {test_users_file}")
    except Exception as e:
        logger.error(f"Failed to load test users: {e}")
        return

    rooms_per_user = room_count // len(setup_users) if len(setup_users) > 0 else 0
    if rooms_per_user * len(setup_users) < room_count:
        rooms_per_user += 1

    generator = TestDataGenerator(host, setup_users, external_users_csv_file)

    try:
        logger.info("=" * 50)
        logger.info("STEP 1: AUTHENTICATING SETUP USERS")
        logger.info("=" * 50)
        generator.login_setup_users()

        if not generator.clients:
            logger.error("No setup users authenticated. Cannot proceed.")
            return

        if room_count > 0:
            logger.info("=" * 50)
            logger.info(f"STEP 2: CREATING {room_count} TEST ROOMS")
            logger.info("=" * 50)
            generator.create_rooms(rooms_per_user)

            logger.info("=" * 50)
            logger.info("STEP 3: ADDING USERS VIA AUDIENCES API")
            logger.info("=" * 50)
            generator.add_users_via_audiences_api()
        else:
            logger.info("Skipping room creation (--rooms=0)")

        logger.info("=" * 50)
        logger.info(f"STEP 4: GENERATING MESSAGE HISTORY")
        logger.info("=" * 50)
        generator.generate_message_history(messages_per_room, reactions_per_room)

        logger.info("=" * 50)
        logger.info("STEP 5: ADDING READ RECEIPTS")
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
        else:
            logger.info(f"  • No new rooms created (--rooms=0)")
        logger.info(
            f"  • Generated messages: ~{len(generator.created_rooms) * messages_per_room}"
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
        description="Setup test data for Matrix load testing (Connect v3)"
    )
    parser.add_argument(
        "--host",
        type=str,
        help="Matrix homeserver URL (e.g., http://localhost:8008 or https://matrix.example.com)",
    )
    parser.add_argument(
        "--setup-users",
        type=str,
        default="setup-users.csv",
        help="CSV file containing setup users who will create rooms/messages (default: setup-users.csv)",
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
