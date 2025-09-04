#!/usr/bin/env python3

"""
Browser-Based OIDC Login Test for Matrix Locust

This script opens a browser for NitroID authentication and captures the login token.
More reliable than form parsing since it uses the actual browser flow.

Usage:
    poetry run python test_real_oidc_browser.py
"""

from gevent import monkey
monkey.patch_all()

import csv
import json
import logging
import os
import time
from contextlib import contextmanager

from nio.responses import LoginError, LoginResponse

from matrix_locust.nio.locust_client import LocustClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

logging.getLogger("nio.events.misc").setLevel(logging.ERROR)


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
        print(f"Request failed: {message}")


class HostContainer:
    def __init__(self, host):
        self.host = host

    @contextmanager
    def rest(self, method, url, headers=None, json=None, name=None):
        import requests

        if headers is None:
            headers = {}
        headers.setdefault("Content-Type", "application/json")

        # Construct full URL if we got a relative path
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
            print(f"Request exception: {e}")
            raise


def test_oidc_login_for_user(homeserver_url, username, password, oidc_issuer, oidc_client_id):
    """Test programmatic OIDC login for a specific user."""

    logger.info(f"Testing OIDC login for user: {username}")
    logger.info(f"Homeserver: {homeserver_url}")

    try:
        host_container = HostContainer(homeserver_url)
        import uuid
        device_id = f"LOCUSTTEST_{uuid.uuid4().hex[:8]}"
        client = LocustClient(
            locust_user=host_container,
            user=username,
            device_id=device_id,
        )

        logger.info(f"Performing OIDC login for {username}...")
        response = client.login_oidc(
            oidc_issuer=oidc_issuer,
            client_id=oidc_client_id,
            username=username,
            password=password,
        )

        if isinstance(response, LoginResponse):
            print(f"✅ Matrix Login successful!")
            print(f"👤 User ID: {response.user_id}")
            print(f"🔑 Access token received")
            print(f"📱 Device ID: {response.device_id}")

            logger.info("Matrix Login successful!")
            logger.info(f"User ID: {response.user_id}")
            logger.info("Access token received")
            logger.info(f"Device ID: {response.device_id}")

            logger.info(f"Client user_id after login: {client.user_id}")
            logger.info(f"Client access_token exists: {bool(client.access_token)}")

            logger.info("Testing authenticated sync request...")
            sync_response = client.sync(timeout=1000)
            if hasattr(sync_response, "next_batch"):
                logger.info("Sync request successful!")
                logger.info(f"Next batch token: {sync_response.next_batch}")
            else:
                logger.warning("Sync request failed or returned unexpected response")

            logger.info("Joining test room...")
            room_id = "!tMcPcABvDSfsxjPtNe:powerapp.cloud"

            join_response = client.join(room_id)
            if hasattr(join_response, "room_id"):
                logger.info(f"Successfully joined room: {join_response.room_id}")
            else:
                logger.warning("Failed to join room or returned unexpected response")

            logger.info("Sending message to test room...")
            message_content = {
                "msgtype": "m.text",
                "body": "hello there"
            }

            message_response = client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=message_content
            )

            if hasattr(message_response, "event_id"):
                print(f"📨 Message sent successfully by {response.user_id}: {message_response.event_id}")
                print(f"💬 Message content: {message_content['body']}")
                logger.info(f"Message sent successfully: {message_response.event_id}")
                logger.info(f"Message: {message_content['body']}")
            else:
                print(f"❌ Message send failed for {response.user_id}")
                logger.warning("Message send failed or returned unexpected response")

            logger.info("Logging out...")
            logout_response = client.logout()
            if hasattr(logout_response, "status"):
                logger.info("Logout successful!")

            return True

        elif isinstance(response, LoginError):
            logger.error(f"Matrix Login failed: {response.message}")
            logger.error(f"Status code: {response.status_code}")
            return False
        else:
            logger.error(f"Unexpected response type: {type(response)}")
            return False

    except Exception as e:
        logger.error(f"Test failed with exception: {str(e)}")
        import traceback

        traceback.print_exc()
        return False


def test_all_users_from_csv(homeserver_url="http://localhost:8008", csv_file="users.csv"):
    """Test OIDC login and message sending for all users in CSV file."""

    if not os.path.exists(csv_file):
        logger.error(f"CSV file {csv_file} not found")
        return False

    users = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append(row)

    if not users:
        logger.error(f"No users found in {csv_file}")
        return False

    logger.info(f"Found {len(users)} users in {csv_file}")

    success_count = 0
    for i, user in enumerate(users, 1):
        username = user.get('username', f'user_{i}')
        password = user.get('password', '')
        oidc_issuer = user.get('oidc_issuer', '')
        oidc_client_id = user.get('oidc_client_id', '')

        if not password or not oidc_issuer:
            logger.error(f"Missing password or OIDC config for user {username}")
            continue

        logger.info(f"Testing user {i}/{len(users)}: {username}")
        logger.info("-" * 40)

        success = test_oidc_login_for_user(homeserver_url, username, password, oidc_issuer, oidc_client_id)
        if success:
            success_count += 1
            logger.info(f"User {username} test completed successfully")
        else:
            logger.error(f"User {username} test failed")

        logger.info("-" * 40)

        if i < len(users):
            logger.info("Waiting 2 seconds before next user...")
            time.sleep(2)

    logger.info("=" * 50)
    logger.info(f"Overall results: {success_count}/{len(users)} users successful")
    return success_count == len(users)


if __name__ == "__main__":
    logger.info("Starting Browser-Based OIDC Login Test for All Users")
    logger.info("=" * 60)

    success = test_all_users_from_csv("https://pr920.connect-server.beta.px.powerapp.cloud")

    logger.info("=" * 60)
    if success:
        logger.info("All user tests completed successfully!")
    else:
        logger.error("Some user tests failed!")
