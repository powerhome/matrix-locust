#!/bin/env python3

import csv
import logging
import resource

from locust import task, constant
from locust import events
from locust.runners import MasterRunner, WorkerRunner

import gevent
from matrix_locust.users.matrixuser import MatrixUser
from nio.responses import LoginError

# Preflight ####################################################################

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
        environment.runner.register_message("load_users", MatrixOIDCLoginUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(environment.runner, MasterRunner):
        # Open our list of users
        MatrixOIDCLoginUser.worker_users = csv.DictReader(open("users.csv"))

################################################################################


class MatrixOIDCLoginUser(MatrixUser):
    wait_time = constant(0)
    worker_id = None
    worker_users = []

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixOIDCLoginUser.worker_users = iter(msg.data)
        MatrixOIDCLoginUser.worker_id = environment.runner.client_id
        logging.info("Worker [%s] Received %s users", environment.runner.client_id, len(msg.data))

    @task
    def oidc_login_user(self):
        # Multiple locust users re-use the same class instance, so need to reset the state
        self.reset_client()

        # Load the next user who needs to be logged in
        try:
            user = next(MatrixOIDCLoginUser.worker_users)
        except StopIteration:
            # We can't shut down the worker until all users are logged in, so return
            # early to stop this individual co-routine
            gevent.sleep(999999)
            return

        # Check if this is an OIDC user file or traditional user file
        if 'oidc_issuer' in user:
            self.login_from_csv_oidc(user)
            use_oidc = True
        else:
            self.login_from_csv(user)
            use_oidc = False

        if self.matrix_client.user is None:
            logging.error("Couldn't get username. Skipping...")
            return

        # Log in as this current user if not already logged in
        if self.matrix_client.user_id is None or self.matrix_client.access_token is None or \
            len(self.matrix_client.user_id) < 1 or len(self.matrix_client.access_token) < 1:

            retries = 3
            while retries > 0:
                if use_oidc:
                    if not hasattr(self.matrix_client, 'oidc_issuer') or self.matrix_client.oidc_issuer is None:
                        logging.error("No OIDC issuer configured. Skipping...")
                        return

                    # Use OIDC login
                    response = self.matrix_client.login_oidc(
                        self.matrix_client.oidc_issuer,
                        getattr(self.matrix_client, 'oidc_client_id', 'matrix-locust')
                    )
                else:
                    # Use password-based login
                    if self.matrix_client.password is None:
                        logging.error("Couldn't get password. Skipping...")
                        return
                    response = self.matrix_client.login(self.matrix_client.password)

                if isinstance(response, LoginError):
                    auth_method = "OIDC" if use_oidc else "password"
                    logging.info("[%s] Could not login user with %s (attempt %d). Trying again...",
                                 self.matrix_client.user, auth_method, 4 - retries)
                    retries -= 1
                    continue

                auth_method = "OIDC" if use_oidc else "password"
                logging.info("[%s] Successfully logged in with %s", self.matrix_client.user, auth_method)
                return

            auth_method = "OIDC" if use_oidc else "password"
            logging.error("Error logging in user %s with %s. Skipping...", self.matrix_client.user, auth_method)
        else:
            logging.info("[%s] User already logged in with access token", self.matrix_client.user)
