#!/usr/bin/env python3

"""
Real OIDC Login Test for Matrix Locust

This script tests REAL OIDC authentication with NitroID for Connect v3.
It performs the actual OIDC flow instead of using mock tokens.

Usage:
    # Set credentials in environment variables (recommended)
    export NITROID_USERNAME=your_username
    export NITROID_PASSWORD=your_password
    python test_real_oidc_login.py

    # If environment variables are not set, you will be prompted for credentials

Requirements:
    - Connect-server running locally at http://localhost:8008
    - OIDC enabled in connect-server configuration
    - Valid NitroID credentials (set as environment variables or provided when prompted)
"""

import argparse
import logging
import sys
import os
import getpass
from matrix_locust.nio.locust_client import LocustClient
from nio.responses import LoginResponse, LoginError

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_credentials():
    """Get NitroID credentials from environment variables or prompt user."""
    username = os.getenv("NITROID_USERNAME")
    password = os.getenv("NITROID_PASSWORD")

    if not username:
        print("NITROID_USERNAME environment variable not set.")
        username = input("Enter NitroID username: ").strip()
        if not username:
            raise ValueError("Username is required")

    if not password:
        print("NITROID_PASSWORD environment variable not set.")
        password = getpass.getpass("Enter NitroID password: ").strip()
        if not password:
            raise ValueError("Password is required")

    return username, password


class HostContainer:
    def __init__(self, host):
        self.host = host


def test_real_oidc_login(homeserver_url: str = "http://localhost:8008"):
    """Test real OIDC login with NitroID."""

    username, password = get_credentials()

    logger.info(f"Testing OIDC login for user: {username}")
    logger.info(f"Homeserver: {homeserver_url}")

    try:
        host_container = HostContainer(homeserver_url)
        client = LocustClient(
            locust_user=host_container,
            user="test",
            device_id="LOCUSTTEST123",
        )

        # Set OIDC credentials
        client.oidc_username = username
        client.oidc_password = password

        logger.info("Starting OIDC authentication flow...")

        # Perform OIDC login
        response = client.login_oidc(
            oidc_issuer="https://id.powerhrg.com",
            client_id="matrix-locust",
            username=username,
            password=password,
        )

        if isinstance(response, LoginResponse):
            logger.info("✅ OIDC Login successful!")
            logger.info(f"User ID: {response.user_id}")
            logger.info(f"Access Token: {response.access_token[:20]}...")
            logger.info(f"Device ID: {response.device_id}")

            # Test a simple authenticated request
            logger.info("Testing authenticated sync request...")
            sync_response = client.sync(timeout=1000)
            if hasattr(sync_response, "next_batch"):
                logger.info("✅ Sync request successful!")
                logger.info(f"Next batch token: {sync_response.next_batch}")
            else:
                logger.warning("⚠️ Sync request failed or returned unexpected response")

            # Logout
            logger.info("Logging out...")
            logout_response = client.logout()
            if hasattr(logout_response, "status"):
                logger.info("✅ Logout successful!")

            return True

        elif isinstance(response, LoginError):
            logger.error(f"❌ OIDC Login failed: {response.message}")
            logger.error(f"Status code: {response.status_code}")
            return False
        else:
            logger.error(f"❌ Unexpected response type: {type(response)}")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed with exception: {str(e)}")
        import traceback

        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Test real OIDC login with matrix-locust"
    )
    parser.add_argument(
        "--homeserver",
        "-s",
        default="http://localhost:8008",
        help="Matrix homeserver URL (default: http://localhost:8008)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)

    logger.info("🚀 Starting Real OIDC Login Test")
    logger.info("=" * 50)
    logger.info(
        "Credentials will be read from NITROID_USERNAME and NITROID_PASSWORD environment variables"
    )
    logger.info("If not set, you will be prompted to enter them")

    success = test_real_oidc_login(args.homeserver)

    logger.info("=" * 50)
    if success:
        logger.info("🎉 Test completed successfully!")
        sys.exit(0)
    else:
        logger.error("💥 Test failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
