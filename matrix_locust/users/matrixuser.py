################################################################################
#
# matrixuser.py - The base MatrixUser class
# -- Extend this class to write your Matrix load test
#
# Created: 2022-08-05
# Author: Charles V Wright <cvwright@futo.org>
# Copyright: 2022 FUTO Holdings Inc
# License: Apache License version 2.0
#
# The MatrixUser class provides a foundational base layer
# in Locust for other Matrix user classes can build on.
# It's sort of like a very minimal Matrix SDK for interacting
# with the homeserver through Locust.  This class aims to
# provide the functionality that would normally be part of
# the client software that a human user would use to interact
# with a Matrix server.  Child classes that inherit from this
# can then focus on mimicking the behavior of the human user.
#
################################################################################

import csv
import os
import sys
import glob
import random
import resource
import json
import logging
from http import HTTPStatus
import mimetypes

from locust import task, between, TaskSet, FastHttpUser
from locust import events
from locust.runners import MasterRunner, WorkerRunner
from collections import namedtuple

import gevent
from matrix_locust.nio.locust_client import LocustClient
from nio.responses import RegisterResponse, LoginResponse, SyncResponse
from typing import Dict


# Locust functions for distributing users to workers ###########################

tokens_dict = {}
if os.path.exists("tokens.csv"):
    with open("tokens.csv", "r", encoding="utf-8") as csvfile:
        csv_header = ["username", "user_id", "access_token", "next_batch"]
        tokens_dict = {
            row["username"]: {
                "user_id": row["user_id"],
                "access_token": row["access_token"],
                "next_batch": row["next_batch"],
            }
            for row in csv.DictReader(csvfile, fieldnames=csv_header)
        }
        tokens_dict.pop("username")  # Dict includes the header values, so remove it

locust_users = []

################################################################################


# Preflight ####################################################################


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    # Increase resource limits to prevent OS running out of descriptors
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    # Register event hooks
    if isinstance(environment.runner, MasterRunner):
        print("Registered 'update_tokens' handler on master worker")
        environment.runner.register_message("update_tokens", update_tokens)


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    global tokens_dict
    csv_header = ["username", "user_id", "access_token", "next_batch"]

    # Write changes to tokens.csv
    with open("tokens.csv", "w", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=csv_header)
        writer.writeheader()

        for k, v in sorted(tokens_dict.items()):
            writer.writerow(
                {
                    "username": k,
                    "user_id": v["user_id"],
                    "access_token": v["access_token"],
                    "next_batch": v["next_batch"],
                }
            )


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    global locust_users
    if isinstance(environment.runner, MasterRunner):
        print("Loading users and sending to workers")
        with open("users.csv", "r", encoding="utf-8") as csvfile:
            user_reader = csv.DictReader(csvfile)
            locust_users = [user for user in user_reader]

            # Divide up users between all workers
            for client_id, index in environment.runner.worker_indexes.items():
                user_count = int(
                    len(locust_users) / environment.runner.worker_index_max
                )
                remainder = (
                    0
                    if index != environment.runner.worker_index_max - 1
                    else (len(locust_users) % environment.runner.worker_index_max)
                )

                start = index * user_count
                end = start + user_count + remainder
                users = locust_users[start:end]

                print(f"Sending {len(users)} users to {client_id}")
                environment.runner.send_message("load_users", users, client_id)


################################################################################


def update_tokens(environment, msg, **_kwargs):
    """Updates the given user's access and sync tokens for writing to the csv file"""
    global tokens_dict
    username = msg.data["username"]
    user_id = msg.data["user_id"]
    access_token = msg.data["access_token"]
    next_batch = msg.data["next_batch"]

    tokens_dict[username] = {
        "user_id": user_id,
        "access_token": access_token,
        "next_batch": next_batch,
    }


class MatrixUser(FastHttpUser):
    # Don't ever directly instantiate this class
    abstract = True

    def wait_time(self):
        return random.expovariate(0.1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reset_client()

    # Review done with UIA implementation
    def _handle_register_response(self, response: RegisterResponse) -> None:
        # manual field updates?
        self.update_tokens()

    # Review done with UIA implementation
    def _handle_login_response(self, response: LoginResponse) -> None:
        # manual field updates?
        self.update_tokens()

    def _handle_sync_response(self, response: SyncResponse) -> None:
        self.update_tokens()

    def reset_client(self):
        """Resets the matrix_client state"""
        self.matrix_client = LocustClient(self)
        self.matrix_client.add_response_callback(
            self._handle_register_response, RegisterResponse
        )
        self.matrix_client.add_response_callback(
            self._handle_login_response, LoginResponse
        )
        self.matrix_client.add_response_callback(
            self._handle_sync_response, SyncResponse
        )

    def set_user(self, user_id):
        """Sets the locust username and host based on user_id"""
        if user_id.find(":") == -1:
            self.matrix_client.user = user_id
        else:
            self.matrix_client.user = user_id[: user_id.find(":")]
            self.matrix_client.matrix_domain = user_id.split(":")[-1]

            protocol = self.matrix_client.locust_user.host[
                : self.matrix_client.locust_user.host.rfind("/") + 1
            ]
            self.matrix_client.locust_user.host = (
                protocol + "matrix." + self.matrix_client.matrix_domain
            )
            self.matrix_client.locust_user.client.base_url = (
                self.matrix_client.locust_user.host
            )

    def login_from_csv(self, user_dict: Dict[str, str]) -> None:
        """Log-in the user from the credentials saved in the csv file

        Args:
            user_dict (dictionary): dictionary of the users.csv file
        """
        global tokens_dict

        self.set_user(user_dict["username"])
        self.matrix_client.password = user_dict["password"]

        if tokens_dict.get(self.matrix_client.user) is not None:
            self.matrix_client.user_id = tokens_dict[self.matrix_client.user].get(
                "user_id"
            )
            self.matrix_client.access_token = tokens_dict[self.matrix_client.user].get(
                "access_token"
            )
            self.matrix_client.next_batch = tokens_dict[self.matrix_client.user].get(
                "next_batch"
            )

        # Handle empty strings
        if (
            len(self.matrix_client.user_id) < 1
            or len(self.matrix_client.access_token) < 1
        ):
            self.matrix_client.user_id = None
            self.matrix_client.access_token = None
            return

        if len(self.matrix_client.next_batch) < 1:
            self.matrix_client.next_batch = None

        self.matrix_client.matrix_domain = self.matrix_client.user_id.split(":")[-1]

    def login_from_csv_oidc(self, user_dict: Dict[str, str]) -> None:
        """Log-in the user from OIDC credentials saved in the csv file

        Args:
            user_dict (dictionary): dictionary of the users.csv file with OIDC data
        """
        global tokens_dict

        self.set_user(user_dict["username"])
        oidc_issuer = user_dict.get("oidc_issuer")
        oidc_client_id = user_dict.get("oidc_client_id", "matrix-locust")

        if tokens_dict.get(self.matrix_client.user) is not None:
            self.matrix_client.user_id = tokens_dict[self.matrix_client.user].get(
                "user_id"
            )
            self.matrix_client.access_token = tokens_dict[self.matrix_client.user].get(
                "access_token"
            )
            self.matrix_client.next_batch = tokens_dict[self.matrix_client.user].get(
                "next_batch"
            )

        # Handle empty strings
        if self.matrix_client.user_id and len(self.matrix_client.user_id) < 1:
            self.matrix_client.user_id = None
        if self.matrix_client.access_token and len(self.matrix_client.access_token) < 1:
            self.matrix_client.access_token = None
        if self.matrix_client.next_batch and len(self.matrix_client.next_batch) < 1:
            self.matrix_client.next_batch = None

        # Store OIDC configuration for login
        self.matrix_client.oidc_issuer = oidc_issuer
        self.matrix_client.oidc_client_id = oidc_client_id

        # Get NitroID credentials from environment variables
        # This replaces the previous CSV-based password storage for security
        self.matrix_client.oidc_username = os.getenv("NITROID_USERNAME")
        self.matrix_client.oidc_password = os.getenv("NITROID_PASSWORD")

        # Log if credentials are not available
        if not self.matrix_client.oidc_username or not self.matrix_client.oidc_password:
            logging.warning(
                f"NITROID_USERNAME and/or NITROID_PASSWORD environment variables not set for user {self.matrix_client.user}"
            )
            logging.warning("OIDC login will fail without valid credentials")

        if user_dict.get("user_id"):
            self.matrix_client.matrix_domain = user_dict["user_id"].split(":")[-1]

    def update_tokens(self) -> None:
        user_update_request = {
            "username": self.matrix_client.user,
            "user_id": self.matrix_client.user_id,
            "access_token": self.matrix_client.access_token,
            "next_batch": self.matrix_client.next_batch,
        }
        self.environment.runner.send_message("update_tokens", user_update_request)
