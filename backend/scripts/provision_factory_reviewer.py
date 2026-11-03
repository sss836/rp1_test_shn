#!/usr/bin/env python3
"""Explicit, audited database-maintenance provisioning of one factory-only reviewer.

Run inside the configured backend environment with an explicit DB superuser URL
in FACTORY_PROVISION_ADMIN_DATABASE_URL. No existing user/session is impersonated.
The generated initial credential is written once to a caller-selected 0600 file;
no password or hash is printed, logged, or passed on the command line.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy import create_engine, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', default='factory_reviewer')
    parser.add_argument('--display-name', default='工厂审批员')
    parser.add_argument('--reason', required=True)
    parser.add_argument('--credentials-file', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Execute the explicitly requested provisioning')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', args.username):
        parser.error('Invalid new username')
    if not 1 <= len(args.reason.strip()) <= 300:
        parser.error('A concise authorization reason is required')
    if not args.apply:
        print('Dry run: fixed role FACTORY_REVIEWER; no account created. Add --apply to provision.')
        return
    url = os.environ.get('FACTORY_PROVISION_ADMIN_DATABASE_URL')
    if not url:
        parser.error('FACTORY_PROVISION_ADMIN_DATABASE_URL must identify the explicit database maintenance administrator')
    target = args.credentials_file.expanduser().absolute()
    if target.exists() or target.is_symlink():
        parser.error('Credential output already exists; it will not be overwritten')
    if not target.parent.is_dir() or target.parent.is_symlink():
        parser.error('Credential parent must be an existing private directory')
    if target.parent.stat().st_mode & 0o077:
        parser.error('Credential parent directory must have mode 0700')
    password = secrets.token_urlsafe(30)
    password_hash = PasswordHasher().hash(password)
    request_id = 'factory-provision-' + uuid4().hex
    engine = create_engine(url, hide_parameters=True)
    created_file = False
    try:
        with engine.begin() as connection:
            # This fixed SQL entry checks session_user is the DB superuser, creates
            # a new HUMAN reviewer only and appends its real maintenance identity.
            public_id = str(connection.execute(text('SELECT iam.provision_factory_reviewer(:username,:display,:hash,:reason,:request_id)'), {
                'username': args.username, 'display': args.display_name,
                'hash': password_hash, 'reason': args.reason.strip(), 'request_id': request_id,
            }).scalar_one())
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created_file = True
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump({'username': args.username, 'initial_password': password, 'role': 'FACTORY_REVIEWER', 'must_change_password': True, 'user_public_id': public_id, 'request_id': request_id}, output, ensure_ascii=False, indent=2)
                output.write('\n'); output.flush(); os.fsync(output.fileno())
        print(json.dumps({'created': True, 'username': args.username, 'role': 'FACTORY_REVIEWER', 'must_change_password': True, 'credentials_file': str(target), 'request_id': request_id}, ensure_ascii=False))
    except BaseException:
        # Preserve any written credential if commit outcome was uncertain. Never
        # reset or duplicate the account automatically; inspect the audit request.
        print(json.dumps({'created': 'unconfirmed', 'request_id': request_id, 'credentials_file_preserved': created_file}, ensure_ascii=False))
        raise RuntimeError('Factory reviewer provisioning failed or was not confirmed; inspect its audit request without retrying a password reset') from None
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
