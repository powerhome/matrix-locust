#!/bin/env python3

"""
Locust Test: Send Messages to Room

This script creates a Locust test that:
1. Reads users from users.csv with OIDC credentials
2. Performs programmatic OIDC login for each user
3. Joins a hardcoded test room
4. Sends messages to the room
5. Provides Locust web UI with stats and metrics

Usage:
    locust -f locust-send-messages.py --host https://pr920.connect-server.beta.px.powerapp.cloud
"""

import csv
import logging
import queue
import random
import threading
import uuid
from contextlib import contextmanager

from gevent import monkey
monkey.patch_all()

from locust import FastHttpUser, task, between, events
from nio.responses import LoginResponse

from matrix_locust.nio.locust_client import LocustClient

logging.getLogger("nio.events.misc").setLevel(logging.ERROR)

import builtins
original_print = builtins.print

users_data = []
user_queue = queue.Queue()

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    global users_data, user_queue
    try:
        with open("users.csv", "r", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            users_data = [row for row in reader]
            print(f"Loaded {len(users_data)} users from users.csv")

            # Pre-populate the queue with user data - cycle through users if needed
            for i in range(1000):  # Support up to 1000 concurrent users
                user_queue.put(users_data[i % len(users_data)])

    except FileNotFoundError:
        print("ERROR: users.csv file not found!")
        users_data = []


class HostContainer:
    def __init__(self, host):
        self.host = host
        # Create a separate session for each HostContainer to ensure isolation
        import requests
        self.session = requests.Session()

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
            response = self.session.request(
                method=method, url=full_url, headers=headers, json=json
            )

            class MockResponse:
                def __init__(self, response):
                    self._response = response
                    self.status_code = response.status_code
                    self.text = response.text
                    try:
                        self.js = response.json()
                    except:
                        self.js = None
                def success(self):
                    pass
                def failure(self, message):
                    print(f"Request failed: {message}")

            yield MockResponse(response)

        except Exception as e:
            print(f"Request exception: {e}")
            raise


class MatrixMessageUser(FastHttpUser):
    wait_time = between(1, 3)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.matrix_client = None
        self.user_data = None
        self.room_id = "!tMcPcABvDSfsxjPtNe:powerapp.cloud"

    def on_start(self):
        """Called when a user starts. Performs OIDC login and joins room."""
        global user_queue

        try:
            # Get the next user from the queue (thread-safe)
            self.user_data = user_queue.get_nowait()
        except queue.Empty:
            print("No more users available in queue!")
            return
        username = self.user_data.get('username', 'unknown')
        password = self.user_data.get('password', '')
        oidc_issuer = self.user_data.get('oidc_issuer', '')
        oidc_client_id = self.user_data.get('oidc_client_id', '')

        print(f"Starting session for user: {username}")

        try:
            host_container = HostContainer(self.host)
            device_id = f"LOCUSTTEST_{uuid.uuid4().hex[:8]}"
            self.matrix_client = LocustClient(
                locust_user=host_container,
                user=username,
                device_id=device_id,
            )

            response = self.matrix_client.login_oidc(
                oidc_issuer=oidc_issuer,
                client_id=oidc_client_id,
                username=username,
                password=password,
            )

            if isinstance(response, LoginResponse):
                print(f"✅ Login successful for {response.user_id}")

                join_response = self.matrix_client.join(self.room_id)
                if hasattr(join_response, "room_id"):
                    print(f"✅ Joined room: {join_response.room_id}")
                else:
                    print(f"❌ Failed to join room for {username}")

            else:
                print(f"❌ Login failed for {username}")
                self.matrix_client = None

        except Exception as e:
            print(f"❌ Error during login for {username}: {e}")
            self.matrix_client = None

    @task
    def send_message(self):
        """Send a message to the test room."""
        if not self.matrix_client:
            return

        username = self.user_data.get('username', 'unknown') if self.user_data else 'unknown'

        message_content = {
            "msgtype": "m.text",
            "body": f"Hello from {username}! 👋"
        }

        try:
            response = self.matrix_client.room_send(
                room_id=self.room_id,
                message_type="m.room.message",
                content=message_content
            )

            if hasattr(response, "event_id"):
                print(f"📨 Message sent by {username}: {response.event_id}")
            else:
                print(f"❌ Failed to send message for {username}")

        except Exception as e:
            print(f"❌ Error sending message for {username}: {e}")

    def on_stop(self):
        """Called when a user stops. Performs logout."""
        if self.matrix_client:
            username = self.user_data.get('username', 'unknown') if self.user_data else 'unknown'
            try:
                self.matrix_client.logout()
                print(f"👋 Logged out user: {username}")
            except Exception as e:
                print(f"❌ Error during logout for {username}: {e}")
