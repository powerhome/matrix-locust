#!/usr/bin/env python3

"""
Browser-Based OIDC Login Test for Matrix Locust

This script opens a browser for NitroID authentication and captures the login token.
More reliable than form parsing since it uses the actual browser flow.

Usage:
    poetry run python test_real_oidc_browser.py
"""

import webbrowser
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.parse
import time
import logging
import json
from contextlib import contextmanager
from matrix_locust.nio.locust_client import LocustClient
from nio.responses import LoginResponse, LoginError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
        headers.setdefault('Content-Type', 'application/json')
        
        # Construct full URL if we got a relative path
        if url.startswith('/'):
            full_url = self.host + url
        else:
            full_url = url
        
        try:
            response = requests.request(
                method=method,
                url=full_url,
                headers=headers,
                json=json
            )
            
            mock_resp = MockResponse(response)
            yield mock_resp
            
        except Exception as e:
            print(f"Request exception: {e}")
            raise

class OIDCBrowserAuth:
    def __init__(self, matrix_url="http://localhost:8008"):
        self.matrix_url = matrix_url
        self.login_token = None
        self.server = None
        
    def start_callback_server(self):
        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
                
            def do_GET(handler_self):
                query = urllib.parse.urlparse(handler_self.path).query
                params = urllib.parse.parse_qs(query)
                
                if 'loginToken' in params:
                    self.login_token = params['loginToken'][0]
                    handler_self.send_response(200)
                    handler_self.send_header('Content-type', 'text/html')
                    handler_self.end_headers()
                    handler_self.wfile.write(b"""
                        <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                        <h1 style="color: green;">Login Successful!</h1>
                        <p>You can close this window and return to the terminal.</p>
                        </body></html>
                    """)
                else:
                    handler_self.send_response(400)
                    handler_self.send_header('Content-type', 'text/html')
                    handler_self.end_headers()
                    handler_self.wfile.write(b"""
                        <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                        <h1 style="color: red;">Login Failed</h1>
                        <p>No login token received.</p>
                        </body></html>
                    """)
        
        self.server = HTTPServer(('localhost', 8080), CallbackHandler)
        self.server.handle_request()
        
    def login(self):
        server_thread = threading.Thread(target=self.start_callback_server)
        server_thread.daemon = True
        server_thread.start()
        
        time.sleep(0.5)
        
        sso_url = f"{self.matrix_url}/_matrix/client/v3/login/sso/redirect/oidc-nitroid?redirectUrl=http://localhost:8080"
        print(f"Opening browser for NitroID login...")
        print(f"URL: {sso_url}")
        webbrowser.open(sso_url)
        
        print("Please complete login in your browser...")
        print("Waiting for callback (timeout: 120 seconds)...")
        
        server_thread.join(timeout=120)
        
        if self.login_token:
            print("Login token received successfully")
        else:
            print("Timeout - no login token received")
            
        return self.login_token

def test_browser_oidc_login(homeserver_url="http://localhost:8008"):
    """Test browser-based OIDC login with NitroID."""
    
    logger.info(f"Testing browser-based OIDC login")
    logger.info(f"Homeserver: {homeserver_url}")
    
    try:
        auth = OIDCBrowserAuth(homeserver_url)
        login_token = auth.login()
        
        if not login_token:
            logger.error("Failed to obtain login token from browser")
            return False
            
        logger.info("Using login token to authenticate with Matrix...")
        
        host_container = HostContainer(homeserver_url)
        client = LocustClient(
            locust_user=host_container,
            user="test",
            device_id="LOCUSTTEST123",
        )
        
        response = client.login(token=login_token)
        
        if isinstance(response, LoginResponse):
            logger.info("Matrix Login successful!")
            logger.info(f"User ID: {response.user_id}")
            logger.info("Access token received")
            logger.info(f"Device ID: {response.device_id}")
            
            logger.info("Testing authenticated sync request...")
            sync_response = client.sync(timeout=1000)
            if hasattr(sync_response, 'next_batch'):
                logger.info("Sync request successful!")
                logger.info(f"Next batch token: {sync_response.next_batch}")
            else:
                logger.warning("Sync request failed or returned unexpected response")
            
            logger.info("Creating test room...")
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            room_name = f"Test room {timestamp}"
            
            room_response = client.room_create(name=room_name)
            if hasattr(room_response, 'room_id'):
                logger.info(f"Room created successfully: {room_response.room_id}")
                logger.info(f"Room name: {room_name}")
            else:
                logger.warning("Room creation failed or returned unexpected response")
            
            logger.info("Logging out...")
            logout_response = client.logout()
            if hasattr(logout_response, 'status'):
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

if __name__ == "__main__":
    logger.info("Starting Browser-Based OIDC Login Test")
    logger.info("=" * 50)
    
    success = test_browser_oidc_login()
    
    logger.info("=" * 50)
    if success:
        logger.info("Test completed successfully!")
    else:
        logger.error("Test failed!")