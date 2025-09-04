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
parser.add_argument("--from-file", type=str, default=None,
                    help="Read usernames from file (one per line) instead of generating them")

args = parser.parse_args()

if args.from_file:
    if not os.path.exists(args.from_file):
        raise FileNotFoundError(f"Input file {args.from_file} not found")
    
    with open(args.from_file, "r", encoding="utf-8") as f:
        usernames_from_file = [line.strip() for line in f if line.strip()]
    
    if not usernames_from_file:
        raise ValueError(f"No usernames found in file {args.from_file}")
    
    print(f"Read {len(usernames_from_file)} usernames from {args.from_file}")
else:
    usernames_from_file = None

with open(args.output, "w", encoding="utf-8") as csvfile:
    if args.oidc:
        # Check for NitroID credentials in environment variables
        nitroid_username = os.getenv('NITROID_USERNAME')
        nitroid_password = os.getenv('NITROID_PASSWORD')
        
        if not nitroid_username or not nitroid_password:
            print("WARNING: NITROID_USERNAME and/or NITROID_PASSWORD environment variables not set.")
            print("Set them with: export NITROID_USERNAME=your_username NITROID_PASSWORD=your_password")
            print("Users will be generated but OIDC login will fail without credentials.")
        
        fieldnames = ["username", "password", "oidc_issuer", "oidc_client_id", "user_id"]
    else:
        fieldnames = ["username", "password"]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
    
    if usernames_from_file:
        iterations = enumerate(usernames_from_file)
    else:
        iterations = enumerate(range(args.num_users))
    
    for i, user_data in iterations:
        host = ""

        if args.domains is not None:
             host = random.choices(args.domains, args.weights)[0]

        if args.oidc:
            if args.oidc_issuer is None:
                raise ValueError("OIDC issuer URL is required when using --oidc")
            
            if usernames_from_file:
                username = user_data
            else:
                if nitroid_username:
                    base_username = nitroid_username.split('@')[0] if '@' in nitroid_username else nitroid_username
                    if args.num_users == 1:
                        username = base_username
                    else:
                        username = f"{base_username}.{i:03d}"
                else:
                    username = "user.{:06d}".format(i)
            
            user_id = f"@{username}"
            
            password = nitroid_password if nitroid_password else "changeme123"
            
            print(f"username = [{username}]\tpassword = [{password}]\toidc_issuer = [{args.oidc_issuer}]\tuser_id = [{user_id}]")
            writer.writerow({
                "username": username,
                "password": password,
                "oidc_issuer": args.oidc_issuer,
                "oidc_client_id": args.oidc_client_id,
                "user_id": user_id
            })
        else:
            if usernames_from_file:
                username = user_data
            else:
                username = "user.{:06d}".format(i)
            
            password = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", k=16))
            print(f"username = [{username}]\tpassword = [{password}]")

            writer.writerow({"username": username, "password": password})
