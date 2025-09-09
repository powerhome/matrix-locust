#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()

import csv
import json
import logging
import random
import resource
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from typing import Dict, Optional

import gevent
from locust import HttpUser, task, between, events
from locust.runners import MasterRunner, WorkerRunner
from nio.responses import LoginError, LoginResponse, RoomMessagesError

from matrix_locust.nio.locust_client import LocustClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce nio's logging verbosity to avoid noise
logging.getLogger('nio.rooms').setLevel(logging.WARNING)
logging.getLogger('nio.responses').setLevel(logging.WARNING)
logging.getLogger('nio.client').setLevel(logging.WARNING)

@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "--sync-type",
        choices=["standard", "lazy-loading"],
        default="standard",
        help="Sync method to use: standard (basic sync) or lazy-loading (filtered sync with lazy_load_members)"
    )
    parser.add_argument(
        "--enable-background-sync",
        choices=["true", "false"],
        default="false",
        help="Enable continuous background sync loop (disabled by default for deterministic tests)"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of task iterations each user will perform (default: 1)"
    )

test_rooms = []
user_pool = []
sync_type = "standard"
background_sync_enabled = False
iterations = 1

# Global metrics tracking
login_metrics = {
    'successful_logins': 0,
    'failed_logins': 0,
    'sync_errors': 0,
    'total_users': 0,
    'successful_login_times': [],
    'failed_login_times': [],
    'time_to_interactive': []
}

# Sync-specific metrics for custom charts
sync_metrics = {
    'sync_requests_per_second': [],
    'successful_sync_response_times': [],
    'failed_sync_response_times': [],
    'successful_sync_request_count': 0,
    'failed_sync_request_count': 0,
    'last_sync_timestamp': time.time(),
    'last_report_time': time.time()
}

@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (999999, 999999))
    except ValueError as e:
        logging.warning(f"Failed to increase the resource limit: {e}")

    global test_rooms, sync_type, background_sync_enabled, iterations

    if hasattr(environment, 'parsed_options') and hasattr(environment.parsed_options, 'sync_type'):
        sync_type = environment.parsed_options.sync_type
        logger.info(f"Using sync type: {sync_type}")

    if hasattr(environment, 'parsed_options') and hasattr(environment.parsed_options, 'enable_background_sync'):
        background_sync_enabled = environment.parsed_options.enable_background_sync == "true"
        logger.info(f"Background sync enabled: {background_sync_enabled}")

    if hasattr(environment, 'parsed_options') and hasattr(environment.parsed_options, 'iterations'):
        iterations = environment.parsed_options.iterations
        logger.info(f"Iterations per user: {iterations}")

    try:
        with open('test_rooms.json', 'r') as f:
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
            end = start + users_per_worker + (remainder if idx == len(environment.runner.clients) - 1 else 0)

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
            "count": len(times_list)
        }

    successful_login_stats = calculate_stats(login_metrics['successful_login_times'])
    failed_login_stats = calculate_stats(login_metrics['failed_login_times'])
    successful_sync_stats = calculate_stats(sync_metrics['successful_sync_response_times'])
    failed_sync_stats = calculate_stats(sync_metrics['failed_sync_response_times'])
    interactive_stats = calculate_stats(login_metrics['time_to_interactive'])

    logger.info("\n" + "="*80)
    logger.info("PHASE 2 LOAD TEST SUMMARY REPORT")
    logger.info("="*80)
    logger.info(f"Total Users: {len(user_pool)}")
    logger.info(f"Successful Logins: {login_metrics['successful_logins']}")
    logger.info(f"Failed Logins: {login_metrics['failed_logins']}")
    logger.info(f"Sync Errors: {login_metrics['sync_errors']}")
    logger.info(f"Login Success Rate: {(login_metrics['successful_logins'] / max(1, login_metrics['successful_logins'] + login_metrics['failed_logins']) * 100):.1f}%")
    logger.info("-" * 80)
    logger.info("SYNC PERFORMANCE (SUCCESSFUL REQUESTS ONLY):")
    current_sync_rps = sync_metrics['sync_requests_per_second'][-1]['rps'] if sync_metrics['sync_requests_per_second'] else 0
    successful_sync_times = sync_metrics['successful_sync_response_times'][-100:]
    failed_sync_times = sync_metrics['failed_sync_response_times'][-100:]
    avg_successful_sync_time = sum(successful_sync_times) / len(successful_sync_times) if successful_sync_times else 0
    avg_failed_sync_time = sum(failed_sync_times) / len(failed_sync_times) if failed_sync_times else 0
    total_successful_syncs = len(sync_metrics['successful_sync_response_times'])
    total_failed_syncs = len(sync_metrics['failed_sync_response_times'])
    sync_success_rate = (total_successful_syncs / max(1, total_successful_syncs + total_failed_syncs) * 100) if (total_successful_syncs + total_failed_syncs) > 0 else 0

    logger.info(f"  Current Success RPS: {current_sync_rps:.1f}")
    logger.info(f"  Avg Success Response Time: {avg_successful_sync_time:.1f}ms")
    logger.info(f"  Avg Failed Response Time: {avg_failed_sync_time:.1f}ms")
    logger.info(f"  Total Successful Syncs: {total_successful_syncs}")
    logger.info(f"  Total Failed Syncs: {total_failed_syncs}")
    logger.info(f"  Sync Success Rate: {sync_success_rate:.1f}%")
    logger.info("-" * 80)
    logger.info("LOGIN PERFORMANCE (SUCCESSFUL ONLY):")
    logger.info(f"  Average: {successful_login_stats['avg']:.1f}ms")
    logger.info(f"  Min: {successful_login_stats['min']:.1f}ms")
    logger.info(f"  Max: {successful_login_stats['max']:.1f}ms")
    logger.info(f"  Count: {successful_login_stats['count']}")
    if failed_login_stats['count'] > 0:
        logger.info(f"  Failed Login Avg: {failed_login_stats['avg']:.1f}ms ({failed_login_stats['count']} failures)")
    logger.info("-" * 80)
    logger.info("ALL SYNC PERFORMANCE (SUCCESSFUL ONLY):")
    logger.info(f"  Average: {successful_sync_stats['avg']:.1f}ms")
    logger.info(f"  Min: {successful_sync_stats['min']:.1f}ms")
    logger.info(f"  Max: {successful_sync_stats['max']:.1f}ms")
    logger.info(f"  Count: {successful_sync_stats['count']}")
    if failed_sync_stats['count'] > 0:
        logger.info(f"  Failed Sync Avg: {failed_sync_stats['avg']:.1f}ms ({failed_sync_stats['count']} failures)")
    logger.info("-" * 80)
    logger.info("TIME TO INTERACTIVE:")
    logger.info(f"  Average: {interactive_stats['avg']:.1f}ms")
    logger.info(f"  Min: {interactive_stats['min']:.1f}ms")
    logger.info(f"  Max: {interactive_stats['max']:.1f}ms")
    logger.info(f"  Count: {interactive_stats['count']}")
    logger.info("="*80)

def track_sync_request(response_time: float, success: bool = True):
    """Track sync-specific metrics for custom charts."""
    global sync_metrics

    current_time = time.time()

    if success:
        sync_metrics['successful_sync_request_count'] += 1
        sync_metrics['successful_sync_response_times'].append(response_time)
    else:
        sync_metrics['failed_sync_request_count'] += 1
        sync_metrics['failed_sync_response_times'].append(response_time)

    # Calculate requests per second over the last second (successful requests only)
    time_diff = current_time - sync_metrics['last_sync_timestamp']
    if time_diff >= 1.0:  # Update RPS every second
        successful_rps = sync_metrics['successful_sync_request_count'] / time_diff if time_diff > 0 else 0
        sync_metrics['sync_requests_per_second'].append({
            'timestamp': current_time,
            'rps': successful_rps,
            'successful_count': sync_metrics['successful_sync_request_count'],
            'failed_count': sync_metrics['failed_sync_request_count']
        })
        sync_metrics['successful_sync_request_count'] = 0
        sync_metrics['failed_sync_request_count'] = 0
        sync_metrics['last_sync_timestamp'] = current_time

        # Keep only last 300 seconds of data (5 minutes)
        sync_metrics['sync_requests_per_second'] = sync_metrics['sync_requests_per_second'][-300:]

    # Keep only last 1000 response times
    sync_metrics['successful_sync_response_times'] = sync_metrics['successful_sync_response_times'][-1000:]
    sync_metrics['failed_sync_response_times'] = sync_metrics['failed_sync_response_times'][-1000:]

    # Log sync stats every 30 seconds
    if current_time - sync_metrics['last_report_time'] > 30:
        current_rps = sync_metrics['sync_requests_per_second'][-1]['rps'] if sync_metrics['sync_requests_per_second'] else 0
        successful_times = sync_metrics['successful_sync_response_times'][-100:]
        failed_times = sync_metrics['failed_sync_response_times'][-100:]
        avg_successful_response_time = sum(successful_times) / len(successful_times) if successful_times else 0
        avg_failed_response_time = sum(failed_times) / len(failed_times) if failed_times else 0
        total_successful = len(sync_metrics['successful_sync_response_times'])
        total_failed = len(sync_metrics['failed_sync_response_times'])

        logger.info(f"[SYNC STATS] Success RPS: {current_rps:.1f}, Avg Success: {avg_successful_response_time:.1f}ms ({total_successful}), Avg Failed: {avg_failed_response_time:.1f}ms ({total_failed})")
        sync_metrics['last_report_time'] = current_time

@events.init.add_listener
def on_locust_init_charts(environment, **_kwargs):
    """Initialize custom charts for sync metrics."""
    if environment.web_ui:
        @environment.web_ui.app.route("/sync_charts")
        def sync_charts():
            """Custom endpoint to serve sync metrics for charts."""
            global sync_metrics

            # Calculate current RPS
            current_rps = 0
            if sync_metrics['sync_requests_per_second']:
                current_rps = sync_metrics['sync_requests_per_second'][-1]['rps']

            avg_successful_response_time = 0
            if sync_metrics['successful_sync_response_times']:
                recent_times = sync_metrics['successful_sync_response_times'][-100:]
                avg_successful_response_time = sum(recent_times) / len(recent_times)

            return {
                "sync_rps": current_rps,
                "sync_avg_response_time": avg_successful_response_time,
                "sync_rps_history": sync_metrics['sync_requests_per_second'][-60:],
                "sync_response_time_history": sync_metrics['successful_sync_response_times'][-100:],
                "failed_sync_response_time_history": sync_metrics['failed_sync_response_times'][-100:]
            }

        # Add custom stats to Locust's existing charts
        @environment.web_ui.app.route("/extended-stats")
        def extended_stats():
            """Extended stats endpoint for sync-specific metrics."""
            global sync_metrics

            stats = environment.stats

            # Find sync-related stats
            sync_stats = {}
            for name, stat in stats.entries.items():
                if name[1] and ('/sync' in name[1].lower() or 'sync' in name[1].lower()):
                    sync_stats[name[1]] = {
                        'num_requests': stat.num_requests,
                        'num_failures': stat.num_failures,
                        'avg_response_time': stat.avg_response_time,
                        'min_response_time': stat.min_response_time or 0,
                        'max_response_time': stat.max_response_time,
                        'current_rps': stat.current_rps,
                        'total_rps': stat.total_rps
                    }

            return {
                "sync_specific_stats": sync_stats,
                "custom_sync_metrics": {
                    "current_rps": sync_metrics['sync_requests_per_second'][-1]['rps'] if sync_metrics['sync_requests_per_second'] else 0,
                    "avg_successful_response_time": sum(sync_metrics['successful_sync_response_times'][-100:]) / len(sync_metrics['successful_sync_response_times'][-100:]) if sync_metrics['successful_sync_response_times'] else 0,
                    "avg_failed_response_time": sum(sync_metrics['failed_sync_response_times'][-100:]) / len(sync_metrics['failed_sync_response_times'][-100:]) if sync_metrics['failed_sync_response_times'] else 0
                }
            }

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
        self._session = None

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            # Configure session with connection pooling
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=500,
                pool_maxsize=500,
                max_retries=3
            )
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
        return self._session

    @contextmanager
    def rest(self, method, url, headers=None, json=None, name=None):
        if headers is None:
            headers = {}
        headers.setdefault("Content-Type", "application/json")

        if url.startswith("/"):
            full_url = self.host + url
        else:
            full_url = url

        response = None
        try:
            response = self.session.request(
                method=method, url=full_url, headers=headers, json=json
            )

            mock_resp = MockResponse(response)
            yield mock_resp

        except Exception as e:
            logger.error(f"Request exception: {e}")
            raise
        finally:
            if response is not None:
                response.close()

    def close(self):
        if self._session:
            self._session.close()

class AppleClientUser(HttpUser):
    wait_time = lambda self: 2
    _user_counter = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.matrix_client: Optional[LocustClient] = None
        self.sync_token: Optional[str] = None
        self.initial_sync_complete = False
        self.initial_sync_start_time = None
        self.initial_sync_duration = None
        self.time_to_interactive = None
        self.sync_task = None
        self.room_states: Dict[str, Dict] = {}
        self.loaded_members: set = set()
        self.device_id = None
        self.username = None
        self.host_container = None
        self.raw_sync_enabled = True
        self.sync_type = self.environment.parsed_options.sync_type if hasattr(self.environment.parsed_options, 'sync_type') else "standard"

    def run(self):
        self.on_start()

        current_iterations = self.environment.parsed_options.iterations if hasattr(self.environment.parsed_options, 'iterations') else 1
        for iteration in range(current_iterations):
            logger.info(f"[{self.username}] Performing foreground sync {iteration + 1}/{current_iterations}")
            self.simulate_app_foreground()
            gevent.sleep(2)

        logger.info(f"[{self.username}] Completed all {current_iterations} iterations")
        self.on_stop()

    def on_start(self):
        global user_pool

        if not user_pool:
            logger.error("No users available for testing")
            return

        user_index = AppleClientUser._user_counter % len(user_pool)
        AppleClientUser._user_counter += 1
        user_data = user_pool[user_index]
        self.username = user_data.get('username')
        password = user_data.get('password')
        oidc_issuer = user_data.get('oidc_issuer')
        oidc_client_id = user_data.get('oidc_client_id', 'matrix-locust')

        if not password or not oidc_issuer:
            logger.error(f"Missing OIDC credentials for user {self.username}")
            return

        self.device_id = f"iOS_{uuid.uuid4().hex[:8]}"

        logger.info(f"[{self.username}] Starting Connect Apple client simulation")

        self.initial_sync_start_time = time.time()

        max_login_attempts = 3
        login_successful = False

        for attempt in range(max_login_attempts):
            if self._perform_login(self.username, password, oidc_issuer, oidc_client_id):
                login_successful = True
                break
            logger.warning(f"[{self.username}] Login attempt {attempt+1} failed, retrying...")
            gevent.sleep(2)

        if login_successful:
            self._perform_post_login_setup()
            self._perform_initial_sync()

            if self.initial_sync_complete:
                self.time_to_interactive = time.time() - self.initial_sync_start_time
                logger.info(f"[{self.username}] Time to interactive: {self.time_to_interactive:.2f}s")

                # Track metrics
                global login_metrics
                login_metrics['time_to_interactive'].append(self.time_to_interactive * 1000)

                self.environment.events.request.fire(
                    request_type="iOS_LOGIN",
                    name="time_to_interactive",
                    response_time=self.time_to_interactive * 1000,
                    response_length=0,
                    exception=None,
                    context={}
                )

                current_background_sync = self.environment.parsed_options.enable_background_sync == "true" if hasattr(self.environment.parsed_options, 'enable_background_sync') else False
                if current_background_sync:
                    self.sync_task = gevent.spawn(self._sync_loop)
                else:
                    self.sync_task = None
        else:
            logger.error(f"[{self.username}] All login attempts failed")

    def _perform_login(self, username: str, password: str, oidc_issuer: str, oidc_client_id: str) -> bool:
        global login_metrics
        start_time = time.time()

        try:
            self.host_container = HostContainer(self.host)
            self.matrix_client = LocustClient(
                locust_user=self.host_container,
                user=username,
                device_id=self.device_id,
            )

            response = self.matrix_client.login_oidc(
                oidc_issuer=oidc_issuer,
                client_id=oidc_client_id,
                username=username,
                password=password,
            )

            login_time = (time.time() - start_time) * 1000

            if isinstance(response, LoginResponse):
                logger.info(f"[{username}] OIDC login successful in {login_time:.0f}ms")

                login_metrics['successful_logins'] += 1
                login_metrics['successful_login_times'].append(login_time)

                self.environment.events.request.fire(
                    request_type="OIDC",
                    name="login",
                    response_time=login_time,
                    response_length=0,
                    exception=None,
                    context={}
                )
                return True
            elif isinstance(response, LoginError):
                logger.error(f"[{username}] OIDC login failed: {response.message}")

                login_metrics['failed_logins'] += 1
                login_metrics['failed_login_times'].append(login_time)

                self.environment.events.request.fire(
                    request_type="OIDC",
                    name="login",
                    response_time=login_time,
                    response_length=0,
                    exception=Exception(response.message),
                    context={}
                )
                return False
            else:
                logger.error(f"[{username}] Unexpected login response: {type(response)}")
                return False

        except Exception as e:
            login_time = (time.time() - start_time) * 1000
            logger.error(f"[{username}] Login exception: {str(e)}")

            login_metrics['failed_logins'] += 1
            login_metrics['failed_login_times'].append(login_time)

            self.environment.events.request.fire(
                request_type="OIDC",
                name="login",
                response_time=login_time,
                response_length=0,
                exception=e,
                context={}
            )
            return False

    def _perform_post_login_setup(self):
        """Perform the API calls that Connect Apple makes after successful login."""
        if not self.matrix_client:
            return

        start_time = time.time()

        try:
            with self.host_container.rest("GET", "/_matrix/client/r0/capabilities") as response:
                pass

            self.lazy_loading_filter = {
                "account_data": {
                    "not_types": [
                        "m.push_rules"
                    ]
                },
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
                            "uk.half-shot.bridge"
                        ]
                    },
                    "timeline": {
                        "limit": 1
                    }
                }
            }

            user_id_encoded = self.matrix_client.user.replace(":", "%3A").replace("@", "%40")
            with self.host_container.rest("GET", f"/_matrix/client/r0/profile/{user_id_encoded}/displayname") as response:
                pass

            with self.host_container.rest("GET", f"/_matrix/client/r0/profile/{user_id_encoded}/avatar_url") as response:
                pass

            with self.host_container.rest("GET", "/_matrix/client/v3/capabilities", headers={"Authorization": f"Bearer {self.matrix_client.access_token}"}) as response:
                pass

            with self.host_container.rest("GET", "/_matrix/client/versions") as response:
                pass

            setup_time = (time.time() - start_time) * 1000
            logger.info(f"[{self.username}] Post-login setup completed in {setup_time:.0f}ms, filter ready")

            self.environment.events.request.fire(
                request_type="SETUP",
                name="post_login_setup",
                response_time=setup_time,
                response_length=0,
                exception=None,
                context={"filter_ready": True}
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
                context={}
            )


    def _perform_initial_sync(self):
        global login_metrics
        if not self.matrix_client:
            return

        logger.info(f"[{self.username}] Starting initial sync ({self.sync_type}) with raw HTTP request")

        start_time = time.time()

        try:
            headers = {
                "Authorization": f"Bearer {self.matrix_client.access_token}",
                "Content-Type": "application/json"
            }

            if self.sync_type == "lazy-loading":
                filter_json = json.dumps(self.lazy_loading_filter)
                filter_encoded = urllib.parse.quote(filter_json)
                sync_url = f"/_matrix/client/r0/sync?filter={filter_encoded}&set_presence=online&timeout=0"
            else:
                sync_url = "/_matrix/client/r0/sync?&set_presence=online&timeout=0"

            with self.host_container.rest(
                "GET",
                sync_url,
                headers=headers,
                name=f"initial_sync_{self.sync_type}"
            ) as response:
                sync_duration = time.time() - start_time

                if response.status_code != 200:
                    error_msg = f"Initial sync failed with status {response.status_code}"
                    if hasattr(response, 'text'):
                        error_msg += f" (response: {response.text[:500]})"

                    login_metrics['sync_errors'] += 1
                    track_sync_request(sync_duration * 1000, success=False)

                    logger.error(f"[{self.username}] {error_msg}")

                    self.environment.events.request.fire(
                        request_type="SYNC",
                        name=f"initial_sync_{self.sync_type}",
                        response_time=sync_duration * 1000,
                        response_length=len(response.text) if hasattr(response, 'text') else 0,
                        exception=Exception(error_msg),
                        context={}
                    )

                    self.sync_token = "dummy_token"
                    self.initial_sync_complete = True
                    return

                try:
                    if hasattr(response, 'js') and response.js and 'next_batch' in response.js:
                        self.sync_token = response.js['next_batch']
                        self.initial_sync_complete = True
                    else:
                        logger.warning(f"[{self.username}] No sync token in response")
                        self.sync_token = "dummy_token"
                        self.initial_sync_complete = True
                except Exception as parse_error:
                    logger.warning(f"[{self.username}] Failed to parse sync response: {parse_error}")
                    self.sync_token = "dummy_token"
                    self.initial_sync_complete = True

                track_sync_request(sync_duration * 1000, success=True)

                response_length = len(response.text) if hasattr(response, 'text') else 0
                logger.info(f"[{self.username}] Initial sync completed: {sync_duration:.2f}s, {response_length} bytes")

                self.environment.events.request.fire(
                    request_type="SYNC",
                    name=f"initial_sync_{self.sync_type}",
                    response_time=sync_duration * 1000,
                    response_length=response_length,
                    exception=None,
                    context={"response_size": response_length}
                )

        except Exception as e:
            sync_duration = time.time() - start_time
            logger.error(f"[{self.username}] Initial sync exception: {str(e)}")

            login_metrics['sync_errors'] += 1
            track_sync_request(sync_duration * 1000, success=False)

            self.environment.events.request.fire(
                request_type="SYNC",
                name=f"initial_sync_{self.sync_type}",
                response_time=sync_duration * 1000,
                response_length=0,
                exception=e,
                context={}
            )

            self.sync_token = "dummy_token"
            self.initial_sync_complete = True


    def _sync_loop(self):
        if not self.matrix_client or not self.sync_token:
            return

        logger.info(f"[{self.username}] Starting background sync loop ({self.sync_type}) with raw HTTP requests")

        while True:
            try:
                start_time = time.time()

                headers = {
                    "Authorization": f"Bearer {self.matrix_client.access_token}",
                    "Content-Type": "application/json"
                }

                if self.sync_type == "lazy-loading":
                    filter_json = json.dumps(self.lazy_loading_filter)
                    filter_encoded = urllib.parse.quote(filter_json)
                    url = f"/_matrix/client/r0/sync?filter={filter_encoded}&set_presence=online&timeout=30000&since={self.sync_token}"
                else:
                    url = f"/_matrix/client/r0/sync?&set_presence=online&timeout=30000&since={self.sync_token}"

                with self.host_container.rest(
                    "GET",
                    url,
                    headers=headers,
                    name=f"background_sync_{self.sync_type}"
                ) as response:
                    sync_duration = time.time() - start_time

                    if response.status_code != 200:
                        error_msg = f"Background sync failed with status {response.status_code}"
                        if hasattr(response, 'text'):
                            error_msg += f" (response: {response.text[:200]})"

                        track_sync_request(sync_duration * 1000, success=False)
                        logger.error(f"[{self.username}] {error_msg}")

                        self.environment.events.request.fire(
                            request_type="SYNC",
                            name=f"background_sync_{self.sync_type}",
                            response_time=sync_duration * 1000,
                            response_length=len(response.text) if hasattr(response, 'text') else 0,
                            exception=Exception(error_msg),
                            context={}
                        )

                        gevent.sleep(5)
                        continue

                    track_sync_request(sync_duration * 1000, success=True)

                    try:
                        if hasattr(response, 'js') and response.js and 'next_batch' in response.js:
                            self.sync_token = response.js['next_batch']
                    except Exception as parse_error:
                        logger.debug(f"[{self.username}] Failed to parse sync token: {parse_error}")

                    response_length = len(response.text) if hasattr(response, 'text') else 0
                    logger.debug(f"[{self.username}] Background sync: {sync_duration:.2f}s, {response_length} bytes")

                    self.environment.events.request.fire(
                        request_type="SYNC",
                        name=f"background_sync_{self.sync_type}",
                        response_time=sync_duration * 1000,
                        response_length=response_length,
                        exception=None,
                        context={"response_size": response_length}
                    )

                gevent.sleep(1.0)

            except Exception as e:
                sync_duration = time.time() - start_time
                logger.error(f"[{self.username}] Sync loop exception: {str(e)}")

                self.environment.events.request.fire(
                    request_type="SYNC",
                    name=f"background_sync_{self.sync_type}",
                    response_time=sync_duration * 1000,
                    response_length=0,
                    exception=e,
                    context={}
                )

                gevent.sleep(5)

    def simulate_app_foreground(self):
        if not self.initial_sync_complete or not self.matrix_client:
            return

        logger.debug(f"[{self.username}] App foreground ({self.sync_type}) - quick sync with raw HTTP")

        start_time = time.time()

        try:
            headers = {
                "Authorization": f"Bearer {self.matrix_client.access_token}",
                "Content-Type": "application/json"
            }

            if self.sync_type == "lazy-loading":
                filter_json = json.dumps(self.lazy_loading_filter)
                filter_encoded = urllib.parse.quote(filter_json)
                url = f"/_matrix/client/r0/sync?filter={filter_encoded}&set_presence=online&timeout=0&since={self.sync_token}"
            else:
                url = f"/_matrix/client/r0/sync?&set_presence=online&timeout=0&since={self.sync_token}"

            with self.host_container.rest(
                "GET",
                url,
                headers=headers,
                name=f"foreground_sync_{self.sync_type}"
            ) as response:
                sync_time = (time.time() - start_time) * 1000

                if response.status_code == 200:
                    track_sync_request(sync_time, success=True)

                    try:
                        if hasattr(response, 'js') and response.js and 'next_batch' in response.js:
                            self.sync_token = response.js['next_batch']
                    except Exception as parse_error:
                        logger.debug(f"[{self.username}] Failed to parse sync token: {parse_error}")

                    response_length = len(response.text) if hasattr(response, 'text') else 0
                    self.environment.events.request.fire(
                        request_type="SYNC",
                        name=f"foreground_sync_{self.sync_type}",
                        response_time=sync_time,
                        response_length=response_length,
                        exception=None,
                        context={"response_size": response_length}
                    )
                else:
                    track_sync_request(sync_time, success=False)
                    error_msg = f"Foreground sync failed with status {response.status_code}"
                    self.environment.events.request.fire(
                        request_type="SYNC",
                        name=f"foreground_sync_{self.sync_type}",
                        response_time=sync_time,
                        response_length=len(response.text) if hasattr(response, 'text') else 0,
                        exception=Exception(error_msg),
                        context={}
                    )

        except Exception as e:
            sync_time = (time.time() - start_time) * 1000
            track_sync_request(sync_time, success=False)
            self.environment.events.request.fire(
                request_type="SYNC",
                name=f"foreground_sync_{self.sync_type}",
                response_time=sync_time,
                response_length=0,
                exception=e,
                context={}
            )

    def view_room(self):
        if not self.initial_sync_complete or not self.matrix_client or not self.matrix_client.rooms:
            return

        room_id = list(self.matrix_client.rooms.keys())[0] if self.matrix_client.rooms else None
        if not room_id:
            return
        logger.debug(f"[{self.username}] Viewing room {room_id}")

        start_time = time.time()

        try:
            response = self.matrix_client.room_messages(
                room_id=room_id,
                start=self.sync_token,
                limit=30,
                direction="b"
            )

            load_time = (time.time() - start_time) * 1000

            if isinstance(response, RoomMessagesError):
                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="view_room",
                    response_time=load_time,
                    response_length=0,
                    exception=Exception(response.message),
                    context={"room_id": room_id}
                )
            else:
                message_count = len(response.chunk) if hasattr(response, 'chunk') else 0

                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="view_room",
                    response_time=load_time,
                    response_length=0,
                    exception=None,
                    context={"room_id": room_id, "messages": message_count}
                )

                if hasattr(response, 'chunk') and response.chunk:
                    latest_event = response.chunk[0]
                    if hasattr(latest_event, 'event_id'):
                        self.matrix_client.update_receipt_marker(room_id, latest_event.event_id)
                        self.room_states[room_id]['read_marker'] = latest_event.event_id

                    for event in response.chunk:
                        if hasattr(event, 'sender') and event.sender not in self.loaded_members:
                            self._lazy_load_member(event.sender)

        except Exception as e:
            load_time = (time.time() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ROOM",
                name="view_room",
                response_time=load_time,
                response_length=0,
                exception=e,
                context={"room_id": room_id}
            )

    def simulate_push_notification_tap(self):
        if not self.initial_sync_complete or not test_rooms:
            return

        room_data = test_rooms[0] if test_rooms else None
        if not room_data:
            return
        room_id = room_data['room_id']

        logger.debug(f"[{self.username}] Push notification tap - opening room {room_id}")

        start_time = time.time()

        try:
            response = self.matrix_client.room_messages(
                room_id=room_id,
                start='',
                limit=30,
                direction="b"
            )

            load_time = (time.time() - start_time) * 1000

            if isinstance(response, RoomMessagesError):
                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="push_notification_room",
                    response_time=load_time,
                    response_length=0,
                    exception=Exception(response.message),
                    context={"room_id": room_id}
                )
            else:
                message_count = len(response.chunk) if hasattr(response, 'chunk') else 0

                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="push_notification_room",
                    response_time=load_time,
                    response_length=0,
                    exception=None,
                    context={"room_id": room_id, "messages": message_count}
                )

        except Exception as e:
            load_time = (time.time() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ROOM",
                name="push_notification_room",
                response_time=load_time,
                response_length=0,
                exception=e,
                context={"room_id": room_id}
            )

    def scroll_timeline(self):
        if not self.initial_sync_complete or not self.matrix_client or not self.matrix_client.rooms:
            return

        room_id = list(self.matrix_client.rooms.keys())[0] if self.matrix_client.rooms else None
        if not room_id:
            return

        if room_id not in self.room_states:
            self.view_room()
            return

        start_time = time.time()

        try:
            response = self.matrix_client.room_messages(
                room_id=room_id,
                start=self.sync_token,
                limit=30,
                direction="b"
            )

            load_time = (time.time() - start_time) * 1000

            if not isinstance(response, RoomMessagesError) and hasattr(response, 'end'):
                self.room_states[room_id]['pagination_token'] = response.end

                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="scroll_timeline",
                    response_time=load_time,
                    response_length=0,
                    exception=None,
                    context={"room_id": room_id}
                )
            else:
                self.environment.events.request.fire(
                    request_type="ROOM",
                    name="scroll_timeline",
                    response_time=load_time,
                    response_length=0,
                    exception=Exception("Scroll failed"),
                    context={"room_id": room_id}
                )

        except Exception as e:
            load_time = (time.time() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ROOM",
                name="scroll_timeline",
                response_time=load_time,
                response_length=0,
                exception=e,
                context={"room_id": room_id}
            )

    def simulate_app_background(self):
        if not self.initial_sync_complete:
            return

        logger.debug(f"[{self.username}] App backgrounded - pausing sync")

        gevent.sleep(30)

        logger.debug(f"[{self.username}] App resumed from background")

        self.simulate_app_foreground()

    def _lazy_load_member(self, user_id: str):
        if user_id in self.loaded_members or not self.matrix_client:
            return

        try:
            displayname = self.matrix_client.get_displayname(user_id)
            if displayname:
                self.loaded_members.add(user_id)
                logger.debug(f"[{self.username}] Lazy loaded member: {user_id}")
        except Exception as e:
            logger.debug(f"[{self.username}] Failed to load member {user_id}: {e}")

    def on_stop(self):
        if self.sync_task:
            gevent.kill(self.sync_task)
        if self.matrix_client:
            try:
                self.matrix_client.logout()
            except:
                pass
        if self.host_container:
            self.host_container.close()
        logger.info(f"[{self.username}] Client stopped")
