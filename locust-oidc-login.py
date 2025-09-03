#!/bin/env python3

"""
OIDC Login Test for Matrix Locust

This script tests OIDC authentication for Matrix homeservers.
It can be used with users generated using the --oidc flag in generate_users.py

Usage:
    locust -f locust-oidc-login.py --host https://matrix.example.com

The script expects a users.csv file with OIDC user data.
"""

import csv
import logging
import resource

from locust import task, between, FastHttpUser
from locust import events
from locust.runners import MasterRunner, WorkerRunner

import gevent
from matrix_locust.users.matrixuser import MatrixUser
from nio.responses import LoginError

# Global variables for user management
locust_users = []
worker_users = []

# Preflight ####################################################################

@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    # Increase resource limits to prevent OS running out of descriptors
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    # Multi-worker setup
    if isinstance(environment.runner, WorkerRunner):
        print(f"Registered 'load_users' handler on {environment.runner.client_id}")
        environment.runner.register_message("load_users", load_users_handler)
    # Single-worker setup
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(environment.runner, MasterRunner):
        global worker_users
        with open("users.csv", "r", encoding="utf-8") as csvfile:
            user_reader = csv.DictReader(csvfile)
            worker_users = [user for user in user_reader]

@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    global locust_users
    if isinstance(environment.runner, MasterRunner):
        print("Loading OIDC users and sending to workers")
        with open("users.csv", "r", encoding="utf-8") as csvfile:
            user_reader = csv.DictReader(csvfile)
            locust_users = [user for user in user_reader]

            # Divide up users between all workers
            for (client_id, index) in environment.runner.worker_indexes.items():
                user_count = int(len(locust_users) / environment.runner.worker_index_max)
                remainder = 0 if index != environment.runner.worker_index_max - 1 \
                            else (len(locust_users) % environment.runner.worker_index_max)

                start = index * user_count
                end = start + user_count + remainder
                users = locust_users[start:end]

                print(f"Sending {len(users)} OIDC users to {client_id}")
                environment.runner.send_message("load_users", users, client_id)

def load_users_handler(environment, msg, **_kwargs):
    global worker_users
    worker_users = iter(msg.data)
    logging.info("Worker [%s] Received %s OIDC users", environment.runner.client_id, len(msg.data))

################################################################################


class OIDCMatrixUser(MatrixUser):
    """
    Matrix user that authenticates using OIDC instead of passwords.
    
    This class demonstrates how to use OIDC authentication with Matrix homeservers
    in load testing scenarios.
    """
    
    wait_time = between(1, 3)  # Wait 1-3 seconds between tasks
    
    def on_start(self):
        """Called when a simulated user starts running"""
        global worker_users
        
        # Load the next user who needs to be logged in
        try:
            if hasattr(worker_users, '__iter__') and hasattr(worker_users, '__next__'):
                user = next(worker_users)
            else:
                user = worker_users.pop(0) if worker_users else None
                if user is None:
                    raise StopIteration
        except (StopIteration, IndexError):
            # No more users available, sleep to keep the worker alive
            gevent.sleep(999999)
            return

        # Check if this is an OIDC user or traditional user
        if 'oidc_issuer' in user:
            self.login_from_csv_oidc(user)
            self.use_oidc = True
            logging.info(f"Loaded OIDC user: {user['username']} with issuer: {user['oidc_issuer']}")
        else:
            self.login_from_csv(user)
            self.use_oidc = False
            logging.info(f"Loaded traditional user: {user['username']}")

        if self.matrix_client.user is None:
            logging.error("Couldn't get username. Stopping user...")
            return

        # Attempt to log in
        self.perform_login()

    def perform_login(self):
        """Perform the actual login process"""
        # Log in if not already authenticated
        if (self.matrix_client.user_id is None or 
            self.matrix_client.access_token is None or 
            len(self.matrix_client.access_token) < 1):
            
            retries = 3
            while retries > 0:
                try:
                    if self.use_oidc:
                        if not hasattr(self.matrix_client, 'oidc_issuer') or self.matrix_client.oidc_issuer is None:
                            logging.error("No OIDC issuer configured for user %s", self.matrix_client.user)
                            return

                        # Use OIDC login
                        response = self.matrix_client.login_oidc(
                            self.matrix_client.oidc_issuer,
                            getattr(self.matrix_client, 'oidc_client_id', 'matrix-locust')
                        )
                    else:
                        # Use password-based login
                        if self.matrix_client.password is None:
                            logging.error("No password configured for user %s", self.matrix_client.user)
                            return
                        response = self.matrix_client.login(self.matrix_client.password)

                    if isinstance(response, LoginError):
                        auth_method = "OIDC" if self.use_oidc else "password"
                        logging.warning("[%s] Login failed with %s (attempt %d): %s",
                                       self.matrix_client.user, auth_method, 4 - retries, response.message)
                        retries -= 1
                        continue

                    auth_method = "OIDC" if self.use_oidc else "password"
                    logging.info("[%s] Successfully logged in with %s", self.matrix_client.user, auth_method)
                    return

                except Exception as e:
                    auth_method = "OIDC" if self.use_oidc else "password"
                    logging.error("[%s] Exception during %s login (attempt %d): %s",
                                 self.matrix_client.user, auth_method, 4 - retries, str(e))
                    retries -= 1

            auth_method = "OIDC" if self.use_oidc else "password"
            logging.error("Failed to login user %s with %s after 3 attempts", 
                         self.matrix_client.user, auth_method)
        else:
            logging.info("[%s] User already authenticated", self.matrix_client.user)

    @task(1)
    def test_authenticated_endpoint(self):
        """Test an authenticated endpoint to verify login worked"""
        if (self.matrix_client.access_token is None or 
            len(self.matrix_client.access_token) < 1):
            logging.warning("[%s] No access token available, skipping authenticated test", 
                           self.matrix_client.user)
            return

        try:
            # Test sync endpoint to verify authentication
            response = self.matrix_client.sync(timeout=1000)
            if hasattr(response, 'transport_response'):
                status_code = response.transport_response.status_code
                if status_code == 200:
                    logging.debug("[%s] Sync successful", self.matrix_client.user)
                else:
                    logging.warning("[%s] Sync failed with status %d", 
                                   self.matrix_client.user, status_code)
            else:
                logging.debug("[%s] Sync completed", self.matrix_client.user)
        except Exception as e:
            logging.error("[%s] Exception during sync: %s", self.matrix_client.user, str(e))
