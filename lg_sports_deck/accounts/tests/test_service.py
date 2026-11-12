from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest

from lg_sports_deck.core.domain import Database, Engine, Conflict, PermissionDenied
from lg_sports_deck.accounts.service import (Accounts, AuthenticationFailed, RateLimited,
                                              STAFF_GRANTABLE, SESSION_SECONDS)


class Clock:
    def __init__(self):self.t=1_800_000_000
    def __call__(self):return self.t


@pytest.fixture
def world(tmp_path):
    db=Database(tmp_path/'accounts.sqlite3');e=Engine(db);clock=Clock();a=Accounts(db,clock)
    su=a.bootstrap_test_superuser('owner@example.invalid','Fictional Owner','Fictional owner password 42')
    return a,e,su,clock


def make_player(a,n):return a.register_test_player(f'p{n}@example.invalid',f'Player {n}',f'Fictional test password {n} strong')


def test_fictional_domain_hashes_and_session_secrets_are_not_stored(world):
    a,e,su,clock=world;p=make_player(a,1);password='Fictional test password 1 strong'
    with pytest.raises(ValueError):a.register_test_player('real@gmail.com','Real User','Fictional long password')
    with pytest.raises(ValueError):a.register_test_player('x@example.invalid','Bad','short')
    with pytest.raises(Conflict):a.register_test_player('P1@example.invalid','Duplicate','Fictional long password')
    raw,csrf,uid=a.login('P1@example.invalid',password,'fictional-peer-1')
    assert uid==p and a.authenticate(raw).id==p
    with a.db.read() as c:
        user=c.execute('SELECT * FROM users WHERE id=?',(p,)).fetchone()
        sess=c.execute('SELECT * FROM auth_sessions WHERE user_id=?',(p,)).fetchone()
        audit=' '.join(str(tuple(x)) for x in c.execute('SELECT * FROM auth_audit'))
    assert user['password_hash'].startswith('$argon2id$') and password not in user['password_hash']
    assert raw not in tuple(sess) and csrf not in tuple(sess) and password not in audit and raw not in audit
    assert a.csrf_ok(a.authenticate(raw),csrf,csrf) and not a.csrf_ok(a.authenticate(raw),csrf,'different')
    assert not a.csrf_ok(a.authenticate(raw),'é'*43,'é'*43)
    with pytest.raises(Conflict):a.bootstrap_test_superuser('another@example.invalid','Another Owner','Fictional second owner password')
    for mail in ['missing@example.invalid','p1@example.invalid']:
        with pytest.raises(AuthenticationFailed):a.login(mail,'wrong password here','peer-'+mail)


def test_expiry_logout_password_change_and_disable_revoke_sessions(world):
    a,e,su,clock=world;p=make_player(a,2);old='Fictional test password 2 strong'
    raw1,csrf1,_=a.login('p2@example.invalid',old,'peer-a')
    raw2,csrf2,_=a.login('p2@example.invalid',old,'peer-b')
    p1=a.authenticate(raw1);a.logout(p1)
    with pytest.raises(AuthenticationFailed):a.authenticate(raw1)
    a.change_password(a.authenticate(raw2),old,'Changed fictional password 999')
    with pytest.raises(AuthenticationFailed):a.authenticate(raw2)
    with pytest.raises(AuthenticationFailed):a.login('p2@example.invalid',old,'peer-c')
    raw3,_,_=a.login('p2@example.invalid','Changed fictional password 999','peer-d')
    clock.t+=SESSION_SECONDS
    with pytest.raises(AuthenticationFailed):a.authenticate(raw3)
    raw4,_,_=a.login('p2@example.invalid','Changed fictional password 999','peer-d')
    a.disable_test_user(su,p)
    with pytest.raises(AuthenticationFailed):a.authenticate(raw4)
    with pytest.raises(AuthenticationFailed):a.login('p2@example.invalid','Changed fictional password 999','peer-e')


def test_only_superuser_can_grant_revoke_precise_permissions(world):
    a,e,su,clock=world
    admin=a.create_test_staff(su,'admin@example.invalid','Admin Test','Fictional admin password','admin')
    manager=a.create_test_staff(su,'manager@example.invalid','Manager Test','Fictional manager password','manager')
    assert a.permissions(admin)==[] and a.permissions(manager)==[]
    with pytest.raises(PermissionDenied):a.replace_permissions(admin,admin,['queue.manage'])
    with pytest.raises(PermissionDenied):a.create_test_staff(manager,'new@example.invalid','New Staff','Fictional staff password','admin')
    with pytest.raises(ValueError):a.replace_permissions(su,admin,['recordings.view'])
    with pytest.raises(ValueError):a.replace_permissions(su,admin,['staff.permissions'])
    a.replace_permissions(su,admin,['queue.manage','bookings.view'])
    a.require_staff_permission(admin,'queue.manage');a.require_staff_permission(admin,'bookings.view')
    with pytest.raises(PermissionDenied):a.require_staff_permission(admin,'settings.rates.edit')
    assert a.permissions(manager)==[]
    a.replace_permissions(su,admin,[])
    with pytest.raises(PermissionDenied):a.require_staff_permission(admin,'queue.manage')


def test_concurrent_failed_logins_are_throttled_and_window_expires(world):
    a,e,su,clock=world;make_player(a,1)
    def attempt(_):
        try:a.login('p1@example.invalid','wrong fictional password','shared-test-ip');return 'success'
        except AuthenticationFailed:return 'invalid'
        except RateLimited:return 'limited'
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
    assert results.count('invalid')==5 and results.count('limited')==1
    with pytest.raises(RateLimited):a.login('p1@example.invalid','Fictional test password 1 strong','shared-test-ip')
    clock.t+=301
    raw,_,_=a.login('p1@example.invalid','Fictional test password 1 strong','shared-test-ip')
    assert a.authenticate(raw).role=='player'


def test_group_checkout_requires_each_account_notice_and_is_atomic(world):
    a,e,su,clock=world;players=[make_player(a,i) for i in range(4)]
    session=e.create_session(su,'Fictional session','test-token',capacity=4)
    for p in players[:3]:a.accept_test_notice(p)
    with pytest.raises(Conflict):a.test_checkout(players[0],session,players)
    with a.db.read() as c:
        assert c.execute('SELECT count(*) FROM session_registrations').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM auth_test_bookings').fetchone()[0]==0
    a.accept_test_notice(players[3]);result=a.test_checkout(players[0],session,players)
    assert result['amount']==800 and result['state']=='simulated_paid'
    with pytest.raises(Conflict):a.test_checkout(players[0],session,players)


def test_concurrent_simulated_last_place_is_awarded_only_once(world):
    a,e,su,clock=world;players=[make_player(a,i) for i in range(5)]
    session=e.create_session(su,'Fictional session','test-token',capacity=4)
    for p in players:a.accept_test_notice(p)
    a.test_checkout(players[0],session,players[:3])
    def book(p):
        try:a.test_checkout(p,session,[p]);return 'ok'
        except Conflict:return 'full'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(book,players[3:]))
    assert sorted(results)==['full','ok']
    with a.db.read() as c:assert c.execute('SELECT count(*) FROM session_registrations WHERE session_id=?',(session,)).fetchone()[0]==4
