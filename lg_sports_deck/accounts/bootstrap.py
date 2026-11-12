"""One-time OFFLINE bootstrap for the fictional-only account prototype.

Run only against an isolated test DB. Never use real customer data or credentials.
The test password is requested via a terminal prompt, not a CLI argument or log.
"""
from __future__ import annotations
import argparse
import getpass
from lg_sports_deck.core.domain import Database, Engine
from .service import Accounts


def main():
    parser=argparse.ArgumentParser(description='OFFLINE: create the first fictional test Superuser only.')
    parser.add_argument('--db',required=True,help='isolated fictional test SQLite DB')
    parser.add_argument('--email',required=True,help='fictional .invalid email')
    parser.add_argument('--name',default='Fictional Owner')
    parser.add_argument('--session-label',help='optional fictional Open Play fixture label')
    args=parser.parse_args()
    password=getpass.getpass('Fictional TEST password (never use a real password): ')
    if password!=getpass.getpass('Repeat fictional TEST password: '):raise SystemExit('Fictional passwords did not match.')
    db=Database(args.db);accounts=Accounts(db)
    uid=accounts.bootstrap_test_superuser(args.email,args.name,password)
    if args.session_label:
        token=getpass.getpass('Fictional venue token for the sample session: ')
        if not token:raise SystemExit('No fictional token provided; Superuser was created but no session was added.')
        Engine(db).create_session(uid,args.session_label,token,capacity=22)
    print('Fictional bootstrap complete. No production account or credentials were created.')


if __name__=='__main__':main()
