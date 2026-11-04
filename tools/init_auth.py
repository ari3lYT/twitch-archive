#!/usr/bin/env python3
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys


def initialize(directory, username, password):
    if len(password)<14:raise ValueError('Use a password with at least 14 characters')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    salt=secrets.token_hex(16)
    data={'username':username,'salt':salt,'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex(),
          'secret':secrets.token_hex(32),'version':secrets.token_hex(8)}
    target=directory/'auth.json'
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(data,stream)
    return target


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python tools/init_auth.py PANEL_STATE_DIRECTORY')
    username=input('Dashboard username: ').strip()
    if not username:raise SystemExit('A username is required')
    password=getpass.getpass('Dashboard password (14+ characters): ')
    if getpass.getpass('Repeat password: ')!=password:raise SystemExit('Passwords differ')
    print('Created',initialize(sys.argv[1],username,password))
