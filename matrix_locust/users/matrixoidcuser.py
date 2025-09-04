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
        environment.runner.register_message("load_users", MatrixOIDCUser.load_users)
    # Single-worker
    elif not isinstance(environment.runner, WorkerRunner) and not isinstance(
        environment.runner, MasterRunner
    ):
        # Open our list of users
        MatrixOIDCUser.worker_users = csv.DictReader(open("users.csv"))


################################################################################


class MatrixOIDCUser(MatrixUser):
    wait_time = constant(0)
    worker_id = None
    worker_users = []

    @staticmethod
    def load_users(environment, msg, **_kwargs):
        MatrixOIDCUser.worker_users = iter(msg.data)
        MatrixOIDCUser.worker_id = environment.runner.client_id
        logging.info(
            "Worker [%s] Received %s users", environment.runner.client_id, len(msg.data)
        )

    @task
    def oidc_login(self):
        # Multiple locust users re-use the same class instance, so need to reset the state
        self.reset_client()

        # Load the next user who needs to be logged in
        try:
            user = next(MatrixOIDCUser.worker_users)
        except StopIteration:
            # We can't shut down the worker until all users are logged in, so return
            # early to stop this individual co-routine
            gevent.sleep(999999)
            return

        self.login_from_csv_oidc(user)

        if self.matrix_client.user is None:
            logging.error("Couldn't get username. Skipping...")
            return

        if (
            not hasattr(self.matrix_client, "oidc_issuer")
            or self.matrix_client.oidc_issuer is None
        ):
            logging.error("No OIDC issuer configured. Skipping...")
            return

        # Log in as this current user if not already logged in
        if (
            self.matrix_client.user_id is None
            or self.matrix_client.access_token is None
        ):
            retries = 3
            while retries > 0:
                # Use OIDC login instead of password-based login
                response = self.matrix_client.login_oidc(
                    self.matrix_client.oidc_issuer, self.matrix_client.oidc_client_id
                )

                if isinstance(response, LoginError):
                    logging.info(
                        "[%s] Could not login user with OIDC (attempt %d). Trying again...",
                        self.matrix_client.user,
                        4 - retries,
                    )
                    retries -= 1
                    continue

                logging.info(
                    "[%s] Successfully logged in with OIDC", self.matrix_client.user
                )
                return

            logging.error(
                "Error logging in user %s with OIDC. Skipping...",
                self.matrix_client.user,
            )
        else:
            logging.info(
                "[%s] User already logged in with access token", self.matrix_client.user
            )
