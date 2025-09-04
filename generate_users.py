#!/bin/env python3

import argparse
import random
import csv
import os

parser = argparse.ArgumentParser(
    description="Generates a list of matrix users to store in a .csv file")
parser.add_argument("num_users", type=int, default=1000, nargs="?",
                    help="Number of users to generate")
parser.add_argument("-o", "--output", type=str, default="users.csv", nargs="?",
                    help="Output .csv file path")
parser.add_argument("-d", "--domains", default=None,
                    type=lambda s: [str(item) for item in s.replace(" ", "").split(',')],
                    help="Specifies domain(s) for users. Multiple domains must be comma (,) separated.")
parser.add_argument("-w", "--weights", default=None,
                    type=lambda s: [float(item) for item in s.split(',')],
                    help="Comma (,) separated list of weights used for user domain assignment probability")
parser.add_argument("--oidc", action="store_true", default=False,
                    help="Generate users for OIDC authentication. Uses NITROID_USERNAME from environment as base username.")
parser.add_argument("--oidc-issuer", type=str, default=None,
                    help="OIDC issuer URL for authentication")
parser.add_argument("--oidc-client-id", type=str, default="matrix-locust",
                    help="OIDC client ID for authentication")

args = parser.parse_args()

with open(args.output, "w", encoding="utf-8") as csvfile:
    if args.oidc:
        # Check for NitroID credentials in environment variables
        nitroid_username = os.getenv('NITROID_USERNAME')
        nitroid_password = os.getenv('NITROID_PASSWORD')
        
        if not nitroid_username or not nitroid_password:
            print("WARNING: NITROID_USERNAME and/or NITROID_PASSWORD environment variables not set.")
            print("Set them with: export NITROID_USERNAME=your_username NITROID_PASSWORD=your_password")
            print("Users will be generated but OIDC login will fail without credentials.")
        
        fieldnames = ["username", "oidc_issuer", "oidc_client_id", "user_id"]
    else:
        fieldnames = ["username", "password"]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
    for i in range(args.num_users):
        host = ""

        if args.domains is not None:
             host = random.choices(args.domains, args.weights)[0]

        if args.oidc:
            if args.oidc_issuer is None:
                raise ValueError("OIDC issuer URL is required when using --oidc")
            
            # For OIDC, use the real NitroID username as the base username
            if nitroid_username:
                # Use the NitroID username (without @domain if it's an email)
                base_username = nitroid_username.split('@')[0] if '@' in nitroid_username else nitroid_username
                if args.num_users == 1:
                    # Single user - use the exact username
                    username = base_username
                else:
                    # Multiple users - append index to base username
                    username = f"{base_username}.{i:03d}"
                user_id = f"@{username}"
            else:
                # Fallback to generated usernames if no NITROID_USERNAME set
                username = "user.{:06d}".format(i)
                user_id = "@{}".format(username)
            
            print(f"username = [{username}]\toidc_issuer = [{args.oidc_issuer}]\tuser_id = [{user_id}]")
            writer.writerow({
                "username": username, 
                "oidc_issuer": args.oidc_issuer,
                "oidc_client_id": args.oidc_client_id,
                "user_id": user_id
            })
        else:
            # For password-based auth, use generated usernames
            username = "user.{:06d}".format(i)
            
            # WARNING: This is not a safe way to generate real passwords!
            #          Do not do this in real life!
            #          Instead, use the Python `secrets` module.
            #          Here we just want a quick way to generate lots of
            #          passwords without eating up our system's entropy pool,
            #          and anyway these are accounts that we are going to
            #          throw away at the end of the test.
            password = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", k=16))
            print(f"username = [{username}]\tpassword = [{password}]")

            # Access token will be populated when the user is registered
            writer.writerow({"username": username, "password": password})
