#!/usr/bin/env python3

from gevent import monkey

monkey.patch_all()

import csv
import json
import logging
import os
import time

import gevent
from locust import HttpUser, events
from locust.runners import MasterRunner, WorkerRunner
from nio.responses import LoginError, LoginResponse, SyncError

from matrix_locust.nio.locust_oidc_client import LocustOIDCClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Reduce nio's logging verbosity to avoid noise
logging.getLogger("nio.rooms").setLevel(logging.WARNING)
logging.getLogger("nio.responses").setLevel(logging.WARNING)
logging.getLogger("nio.client").setLevel(logging.WARNING)


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "--sync-type",
        choices=["standard", "lazy-loading"],
        default="standard",
        help="Sync method to use: standard (basic sync) or lazy-loading (filtered sync with lazy_load_members)",
    )


test_rooms = []
user_pool = []
sync_type = "standard"

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
        if "username" in tokens_dict:
            tokens_dict.pop("username")

# Global metrics tracking
login_metrics = {
    "successful_logins": 0,
    "failed_logins": 0,
    "sync_errors": 0,
    "successful_login_times": [],
    "failed_login_times": [],
    "time_to_interactive": [],
}

# Sync-specific metrics for custom charts
sync_metrics = {
    "sync_requests_per_second": [],
    "successful_sync_response_times": [],
    "failed_sync_response_times": [],
    "successful_sync_request_count": 0,
    "failed_sync_request_count": 0,
    "last_sync_timestamp": time.time(),
    "last_report_time": time.time(),
}


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except (ValueError, ImportError) as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    global test_rooms, sync_type

    if hasattr(environment, "parsed_options") and hasattr(
        environment.parsed_options, "sync_type"
    ):
        sync_type = environment.parsed_options.sync_type
        logger.info(f"Using sync type: {sync_type}")

    try:
        with open("test_rooms.json", "r") as f:
            test_rooms = json.load(f)
            logger.info(f"Loaded {len(test_rooms)} test rooms")
    except FileNotFoundError:
        logger.warning("test_rooms.json not found. Run locust-setup-test-data.py first")
        test_rooms = []

    if isinstance(environment.runner, WorkerRunner):
        environment.runner.register_message("load_users", load_users_handler)
        environment.runner.register_message("load_rooms", load_rooms_handler)
    elif not isinstance(environment.runner, MasterRunner):
        global user_pool
        import csv

        with open("users.csv", "r", encoding="utf-8") as csvfile:
            user_reader = csv.DictReader(csvfile)
            all_users = list(user_reader)
            user_pool = all_users[3:] if len(all_users) > 3 else all_users
            logger.info(f"Loaded {len(user_pool)} users for testing")


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    if isinstance(environment.runner, MasterRunner):
        global test_rooms
        for client_id in environment.runner.clients:
            environment.runner.send_message("load_rooms", test_rooms, client_id)

        import csv

        with open("users.csv", "r", encoding="utf-8") as csvfile:
            user_reader = csv.DictReader(csvfile)
            all_users = list(user_reader)
            test_users = all_users[3:] if len(all_users) > 3 else all_users

        if not environment.runner.clients:
            return

        users_per_worker = len(test_users) // len(environment.runner.clients)
        remainder = len(test_users) % len(environment.runner.clients)

        for idx, client_id in enumerate(environment.runner.clients):
            start = idx * users_per_worker
            end = (
                start
                + users_per_worker
                + (remainder if idx == len(environment.runner.clients) - 1 else 0)
            )

            users_batch = test_users[start:end]
            logger.info(f"Sending {len(users_batch)} users to worker {client_id}")
            environment.runner.send_message("load_users", users_batch, client_id)


def load_users_handler(environment, msg, **_kwargs):
    global user_pool
    user_pool = msg.data
    logger.info(f"Worker received {len(msg.data)} users")


def load_rooms_handler(environment, msg, **_kwargs):
    global test_rooms
    test_rooms = msg.data
    logger.info(f"Worker received {len(msg.data)} test rooms")


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    """Print a summary report when the test stops."""
    global login_metrics

    def calculate_stats(times_list):
        if not times_list:
            return {"avg": 0, "min": 0, "max": 0, "count": 0}
        return {
            "avg": sum(times_list) / len(times_list),
            "min": min(times_list),
            "max": max(times_list),
            "count": len(times_list),
        }

    successful_login_stats = calculate_stats(login_metrics["successful_login_times"])
    failed_login_stats = calculate_stats(login_metrics["failed_login_times"])
    successful_sync_stats = calculate_stats(
        sync_metrics["successful_sync_response_times"]
    )
    failed_sync_stats = calculate_stats(sync_metrics["failed_sync_response_times"])
    interactive_stats = calculate_stats(login_metrics["time_to_interactive"])

    logger.info("\n" + "=" * 80)
    logger.info("CONNECT APPLE LOGIN TEST SUMMARY REPORT")
    logger.info("=" * 80)
    logger.info(f"Total Users: {len(user_pool)}")
    logger.info(f"Successful Logins: {login_metrics['successful_logins']}")
    logger.info(f"Failed Logins: {login_metrics['failed_logins']}")
    logger.info(f"Sync Errors: {login_metrics['sync_errors']}")
    logger.info(
        f"Login Success Rate: {(login_metrics['successful_logins'] / max(1, login_metrics['successful_logins'] + login_metrics['failed_logins']) * 100):.1f}%"
    )
    logger.info("-" * 80)
    logger.info("LOGIN PERFORMANCE (SUCCESSFUL ONLY):")
    logger.info(f"  Average: {successful_login_stats['avg']:.1f}ms")
    logger.info(f"  Min: {successful_login_stats['min']:.1f}ms")
    logger.info(f"  Max: {successful_login_stats['max']:.1f}ms")
    logger.info(f"  Count: {successful_login_stats['count']}")
    if failed_login_stats["count"] > 0:
        logger.info(
            f"  Failed Login Avg: {failed_login_stats['avg']:.1f}ms ({failed_login_stats['count']} failures)"
        )
    logger.info("-" * 80)
    logger.info("SYNC PERFORMANCE (SUCCESSFUL ONLY):")
    logger.info(f"  Average: {successful_sync_stats['avg']:.1f}ms")
    logger.info(f"  Min: {successful_sync_stats['min']:.1f}ms")
    logger.info(f"  Max: {successful_sync_stats['max']:.1f}ms")
    logger.info(f"  Count: {successful_sync_stats['count']}")
    if failed_sync_stats["count"] > 0:
        logger.info(
            f"  Failed Sync Avg: {failed_sync_stats['avg']:.1f}ms ({failed_sync_stats['count']} failures)"
        )
    logger.info("-" * 80)
    logger.info("TIME TO INTERACTIVE:")
    logger.info(f"  Average: {interactive_stats['avg']:.1f}ms")
    logger.info(f"  Min: {interactive_stats['min']:.1f}ms")
    logger.info(f"  Max: {interactive_stats['max']:.1f}ms")
    logger.info(f"  Count: {interactive_stats['count']}")
    logger.info("=" * 80)


def track_sync_request(response_time: float, success: bool = True):
    """Track sync-specific metrics for custom charts."""
    global sync_metrics

    current_time = time.time()

    if success:
        sync_metrics["successful_sync_request_count"] += 1
        sync_metrics["successful_sync_response_times"].append(response_time)
    else:
        sync_metrics["failed_sync_request_count"] += 1
        sync_metrics["failed_sync_response_times"].append(response_time)

    # Calculate requests per second over the last second (successful requests only)
    time_diff = current_time - sync_metrics["last_sync_timestamp"]
    if time_diff >= 1.0:  # Update RPS every second
        successful_rps = (
            sync_metrics["successful_sync_request_count"] / time_diff
            if time_diff > 0
            else 0
        )
        sync_metrics["sync_requests_per_second"].append(
            {
                "timestamp": current_time,
                "rps": successful_rps,
                "successful_count": sync_metrics["successful_sync_request_count"],
                "failed_count": sync_metrics["failed_sync_request_count"],
            }
        )
        sync_metrics["successful_sync_request_count"] = 0
        sync_metrics["failed_sync_request_count"] = 0
        sync_metrics["last_sync_timestamp"] = current_time

        # Keep only last 300 seconds of data (5 minutes)
        sync_metrics["sync_requests_per_second"] = sync_metrics[
            "sync_requests_per_second"
        ][-300:]

    # Keep only last 1000 response times
    sync_metrics["successful_sync_response_times"] = sync_metrics[
        "successful_sync_response_times"
    ][-1000:]
    sync_metrics["failed_sync_response_times"] = sync_metrics[
        "failed_sync_response_times"
    ][-1000:]

    # Log sync stats every 30 seconds
    if current_time - sync_metrics["last_report_time"] > 30:
        current_rps = (
            sync_metrics["sync_requests_per_second"][-1]["rps"]
            if sync_metrics["sync_requests_per_second"]
            else 0
        )
        successful_times = sync_metrics["successful_sync_response_times"][-100:]
        failed_times = sync_metrics["failed_sync_response_times"][-100:]
        avg_successful_response_time = (
            sum(successful_times) / len(successful_times) if successful_times else 0
        )
        avg_failed_response_time = (
            sum(failed_times) / len(failed_times) if failed_times else 0
        )
        total_successful = len(sync_metrics["successful_sync_response_times"])
        total_failed = len(sync_metrics["failed_sync_response_times"])

        logger.info(
            f"[SYNC STATS] Success RPS: {current_rps:.1f}, Avg Success: {avg_successful_response_time:.1f}ms ({total_successful}), Avg Failed: {avg_failed_response_time:.1f}ms ({total_failed})"
        )
        sync_metrics["last_report_time"] = current_time


class AppleClientUser(HttpUser):
    wait_time = lambda self: 2
    _user_counter = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.matrix_client = LocustOIDCClient(locust_user=self)
        self.sync_token = None
        self.initial_sync_complete = False
        self.initial_sync_start_time = None
        self.time_to_interactive = None
        self.sync_task = None
        self.username = None
        self.lazy_loading_filter = None

    def set_user(self, user_id):
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

    def login_from_csv_real_oidc(self, user_dict):
        global tokens_dict

        self.set_user(user_dict["username"])
        self.matrix_client.password = user_dict["password"]
        self.matrix_client.oidc_issuer = user_dict.get("oidc_issuer")
        self.matrix_client.oidc_client_id = user_dict.get("oidc_client_id")

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
        if (
            self.matrix_client.user_id
            and len(self.matrix_client.user_id) < 1
            or self.matrix_client.access_token
            and len(self.matrix_client.access_token) < 1
        ):
            self.matrix_client.user_id = None
            self.matrix_client.access_token = None
            return

        if self.matrix_client.next_batch and len(self.matrix_client.next_batch) < 1:
            self.matrix_client.next_batch = None

    def sync_forever(
        self,
        client_sleep=None,
        timeout=None,
        sync_filter=None,
        since=None,
        full_state=None,
        set_presence=None,
    ):
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

            if not (client_sleep is None):
                gevent.sleep(client_sleep)

    def run(self):
        self.on_start()

        logger.info(f"[{self.username}] Performing foreground sync")
        self.simulate_app_foreground()

        logger.info(f"[{self.username}] Completed foreground sync")
        self.on_stop()

    def on_start(self):
        global user_pool

        if not user_pool:
            logger.error("No users available for testing")
            return

        user_index = AppleClientUser._user_counter % len(user_pool)
        AppleClientUser._user_counter += 1
        user_data = user_pool[user_index]
        self.username = user_data.get("username")

        logger.info(f"[{self.username}] Starting Connect Apple client simulation")
        self.initial_sync_start_time = time.time()

        self.login_from_csv_real_oidc(user_data)

        if self.matrix_client.user is None or self.matrix_client.password is None:
            logger.error(f"Couldn't get OIDC credentials for user {self.username}")
            return

        import uuid

        self.matrix_client.device_id = f"iOS_{uuid.uuid4().hex[:8]}"

        if (
            self.matrix_client.user_id is None
            or self.matrix_client.access_token is None
            or len(self.matrix_client.user_id) < 1
            or len(self.matrix_client.access_token) < 1
        ):
            max_login_attempts = 3
            login_successful = False

            for attempt in range(max_login_attempts):
                response = self.matrix_client.login_oidc(
                    oidc_issuer=self.matrix_client.oidc_issuer,
                    client_id=self.matrix_client.oidc_client_id,
                    username=self.matrix_client.user,
                    password=self.matrix_client.password,
                )

                if isinstance(response, LoginResponse):
                    login_successful = True
                    global login_metrics
                    login_metrics["successful_logins"] += 1
                    break
                elif isinstance(response, LoginError):
                    logger.warning(
                        f"[{self.username}] Login attempt {attempt+1} failed: {response.message}"
                    )
                    login_metrics["failed_logins"] += 1
                    gevent.sleep(2)

            if login_successful:
                self._perform_post_login_setup()
                self._perform_initial_sync()

                if self.initial_sync_complete:
                    self.time_to_interactive = (
                        time.time() - self.initial_sync_start_time
                    )
                    logger.info(
                        f"[{self.username}] Time to interactive: {self.time_to_interactive:.2f}s"
                    )

                    # Track metrics
                    login_metrics["time_to_interactive"].append(
                        self.time_to_interactive * 1000
                    )

                    self.environment.events.request.fire(
                        request_type="iOS_LOGIN",
                        name="time_to_interactive",
                        response_time=self.time_to_interactive * 1000,
                        response_length=0,
                        exception=None,
                        context={},
                    )

            else:
                logger.error(f"[{self.username}] All login attempts failed")

    def _perform_post_login_setup(self):
        """Perform the API calls that Connect Apple makes after successful login."""
        if not self.matrix_client:
            return

        start_time = time.time()

        try:
            self.lazy_loading_filter = {
                "account_data": {"not_types": ["m.push_rules"]},
                "presence": {"types": []},
                "room": {
                    "ephemeral": {"types": []},
                    "state": {
                        "lazy_load_members": True,
                        "not_types": [
                            "com.powerhrg.audience.context.updated",
                            "com.powerhrg.audiences.changed",
                            "com.powerhrg.room.created",
                            "com.powerhrg.room.members",
                            "m.bridge",
                            "m.room.encryption",
                            "m.room.guest_access",
                            "uk.half-shot.bridge",
                        ],
                    },
                    "timeline": {"limit": 1},
                },
            }

            self.matrix_client.get_displayname()
            self.matrix_client.get_avatar()

            setup_time = (time.time() - start_time) * 1000
            logger.info(
                f"[{self.username}] Post-login setup completed in {setup_time:.0f}ms"
            )

            self.environment.events.request.fire(
                request_type="SETUP",
                name="post_login_setup",
                response_time=setup_time,
                response_length=0,
                exception=None,
                context={"filter_ready": True},
            )

        except Exception as e:
            setup_time = (time.time() - start_time) * 1000
            logger.error(f"[{self.username}] Post-login setup failed: {str(e)}")

            self.environment.events.request.fire(
                request_type="SETUP",
                name="post_login_setup",
                response_time=setup_time,
                response_length=0,
                exception=e,
                context={},
            )

    def _perform_initial_sync(self):
        global login_metrics
        if not self.matrix_client:
            return

        current_sync_type = (
            self.environment.parsed_options.sync_type
            if hasattr(self.environment.parsed_options, "sync_type")
            else "standard"
        )
        logger.info(f"[{self.username}] Starting initial sync ({current_sync_type})")

        start_time = time.time()

        try:
            sync_filter = None
            if current_sync_type == "lazy-loading" and self.lazy_loading_filter:
                sync_filter = self.lazy_loading_filter

            response = self.matrix_client.sync(
                timeout=0,
                sync_filter=sync_filter,
                set_presence="online",
                name=f"initial_sync_{current_sync_type}",
            )

            sync_duration = time.time() - start_time

            if hasattr(response, "next_batch"):
                self.sync_token = response.next_batch
                self.initial_sync_complete = True

                track_sync_request(sync_duration * 1000, success=True)
                logger.info(
                    f"[{self.username}] Initial sync completed: {sync_duration:.2f}s"
                )
            else:
                logger.error(f"[{self.username}] Initial sync failed")
                login_metrics["sync_errors"] += 1
                track_sync_request(sync_duration * 1000, success=False)
                self.sync_token = "dummy_token"
                self.initial_sync_complete = True

        except Exception as e:
            sync_duration = time.time() - start_time
            logger.error(f"[{self.username}] Initial sync exception: {str(e)}")

            login_metrics["sync_errors"] += 1
            track_sync_request(sync_duration * 1000, success=False)

            self.sync_token = "dummy_token"
            self.initial_sync_complete = True

    def simulate_app_foreground(self):
        if not self.initial_sync_complete or not self.matrix_client:
            return

        current_sync_type = (
            self.environment.parsed_options.sync_type
            if hasattr(self.environment.parsed_options, "sync_type")
            else "standard"
        )
        logger.debug(
            f"[{self.username}] App foreground ({current_sync_type}) - quick sync"
        )

        start_time = time.time()

        try:
            sync_filter = None
            if current_sync_type == "lazy-loading" and self.lazy_loading_filter:
                sync_filter = self.lazy_loading_filter

            response = self.matrix_client.sync(
                timeout=0,
                sync_filter=sync_filter,
                since=self.sync_token,
                set_presence="online",
                name=f"foreground_sync_{current_sync_type}",
            )

            sync_time = (time.time() - start_time) * 1000

            if hasattr(response, "next_batch"):
                track_sync_request(sync_time, success=True)
                self.sync_token = response.next_batch
            else:
                track_sync_request(sync_time, success=False)
                if hasattr(response, "status_code") and response.status_code >= 500:
                    logger.debug(
                        f"[{self.username}] 5xx error ({response.status_code}), excluding from Locust stats"
                    )
                else:
                    self.environment.events.request.fire(
                        request_type="GET",
                        name=f"foreground_sync_{current_sync_type}",
                        response_time=sync_time,
                        response_length=0,
                        exception=Exception("Sync failed - no next_batch token"),
                        context={},
                    )

        except Exception as e:
            sync_time = (time.time() - start_time) * 1000
            track_sync_request(sync_time, success=False)
            self.environment.events.request.fire(
                request_type="GET",
                name=f"foreground_sync_{current_sync_type}",
                response_time=sync_time,
                response_length=0,
                exception=e,
                context={},
            )

    def on_stop(self):
        if self.sync_task:
            gevent.kill(self.sync_task)
        super().on_stop()
        logger.info(f"[{self.username}] Client stopped")
