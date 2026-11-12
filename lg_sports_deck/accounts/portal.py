"""Authenticated *fictional* API wired to LG's test-domain prototype.

This is not the previously delivered demo portal and not a customer deployment.
Only .invalid accounts are accepted. Real email verification, recovery, MFA,
production TLS, anti-abuse, privacy and independent security review remain open.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import hmac
import re
import secrets
import sqlite3
import time
from typing import Callable, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from lg_sports_deck.core.domain import Conflict, Database, Engine, PermissionDenied
from lg_sports_deck.core.recording import RecorderError, RecordingCoordinator, SyntheticFFmpegBackend
from .service import (Accounts, AuthenticationFailed, Principal, RateLimited,
                      SESSION_SECONDS, STAFF_GRANTABLE, TEST_NOTICE_VERSION)

STATIC=Path(__file__).resolve().parent/'static'


class NoSyntheticRecorder:
    def start(self,court,match,path):raise RecorderError('Synthetic FFmpeg is unavailable in this test environment.')
    def stop(self,handle):raise RecorderError('Synthetic recorder is unavailable.')
    def abort(self,handle):pass


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class EmptyIn(Body):
    pass


class RegisterIn(Body):
    email: str=Field(min_length=3,max_length=254)
    name: str=Field(min_length=1,max_length=80)
    password: str=Field(min_length=1,max_length=128)


class LoginIn(Body):
    email: str=Field(min_length=1,max_length=254)
    password: str=Field(min_length=1,max_length=128)


class StaffIn(RegisterIn):
    role: Literal['admin','manager']


class PermissionsIn(Body):
    permissions: list[str]=Field(max_length=40)


class PasswordIn(Body):
    old_password: str=Field(min_length=1,max_length=128)
    new_password: str=Field(min_length=1,max_length=128)


class BookingIn(Body):
    session_id: StrictInt=Field(gt=0)
    player_ids: list[StrictInt]=Field(min_length=1,max_length=100)


class CheckinIn(Body):
    session_id: StrictInt=Field(gt=0)
    fictional_venue_token: str=Field(min_length=1,max_length=256)


class AssignIn(Body):
    session_id: StrictInt=Field(gt=0)
    court: Literal[1,2]


class ScoreIn(Body):
    score_a: StrictInt=Field(ge=0,le=99)
    score_b: StrictInt=Field(ge=0,le=99)


class RateIn(Body):
    amount: StrictInt=Field(ge=1,le=100000)


def checked(fn):
    try:return fn()
    except AuthenticationFailed as exc:raise HTTPException(401,str(exc)) from exc
    except RateLimited as exc:raise HTTPException(429,str(exc)) from exc
    except PermissionDenied as exc:raise HTTPException(403,str(exc)) from exc
    except Conflict as exc:raise HTTPException(409,str(exc)) from exc
    except RecorderError as exc:raise HTTPException(503,str(exc)) from exc
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    except sqlite3.IntegrityError as exc:raise HTTPException(409,'Conflicting fictional test state.') from exc


def create_app(db_path: str | Path, media_path: str | Path, allowed_origins: set[str],
               *, secure_cookies: bool=True, clock: Callable[[],float]=time.time,
               ffmpeg_path: str | None=None, extra_grantable: frozenset[str]=frozenset()) -> FastAPI:
    if not allowed_origins or any(not o.startswith(('https://','http://127.0.0.1:','http://localhost:')) for o in allowed_origins):
        raise ValueError('Specify exact test origins. Only HTTPS or explicit loopback HTTP origins are permitted.')
    if not secure_cookies and any(not o.startswith(('http://127.0.0.1:','http://localhost:')) for o in allowed_origins):
        raise ValueError('Insecure test cookies may only be used with explicit loopback origins.')
    db=Database(db_path);engine=Engine(db);accounts=Accounts(db,clock,extra_grantable=extra_grantable)
    backend=SyntheticFFmpegBackend(ffmpeg_path) if ffmpeg_path else NoSyntheticRecorder()
    recorder=RecordingCoordinator(engine,backend,media_path)
    app=FastAPI(title='LG Sports Deck Fictional Account Test',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.accounts=accounts;app.state.engine=engine;app.state.recorder=recorder
    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    app.state.allowed_origins=frozenset(allowed_origins)
    session_cookie='__Host-lg_session' if secure_cookies else 'lg_insecure_test_session'
    csrf_cookie='__Host-lg_csrf' if secure_cookies else 'lg_insecure_test_csrf'
    anon_cookie='__Host-lg_anon' if secure_cookies else 'lg_insecure_test_anon'

    @app.middleware('http')
    async def test_security_headers(request,call_next):
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"
        return response

    def origin_guard(request: Request):
        if request.headers.get('origin') not in app.state.allowed_origins:
            raise HTTPException(403,'Untrusted or missing test origin.')

    def anon_guard(request: Request,token: str | None):
        origin_guard(request)
        cookie=request.cookies.get(anon_cookie)
        if (not isinstance(cookie,str) or not isinstance(token,str)
                or not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}',cookie)
                or not re.fullmatch(r'[A-Za-z0-9_\-]{32,128}',token)
                or not hmac.compare_digest(cookie,token)):
            raise HTTPException(403,'Test form expired. Reload before trying again.')

    def principal(request: Request) -> Principal:
        return checked(lambda:accounts.authenticate(request.cookies.get(session_cookie)))

    def action(request: Request,token: str | None) -> Principal:
        origin_guard(request);p=principal(request)
        if not accounts.csrf_ok(p,token,request.cookies.get(csrf_cookie)):
            raise HTTPException(403,'Test session check failed. Sign in again.')
        return p

    def superuser(p: Principal):
        if p.role!='superuser':raise HTTPException(403,'Only the fictional Superuser may perform this action.')

    # Explicit hooks for the isolated unified prototype to reuse the same
    # server-side identity and CSRF checks, without trusting browser role fields.
    app.state.require_principal=principal
    app.state.require_action=action
    app.state.require_superuser=superuser

    def set_cookie(response,key,value,*,httponly,max_age):
        response.set_cookie(key,value,max_age=max_age,path='/',secure=secure_cookies,httponly=httponly,samesite='strict')

    def clear_auth_cookies(response):
        for key,httponly in ((session_cookie,True),(csrf_cookie,False)):
            response.delete_cookie(key,path='/',secure=secure_cookies,httponly=httponly,samesite='strict')

    @app.get('/')
    def index():return FileResponse(STATIC/'index.html',media_type='text/html')

    @app.get('/api/auth/bootstrap')
    def bootstrap():
        token=secrets.token_urlsafe(32)
        response=JSONResponse({'mode':'fictional-only','csrf':token,
            'message':'No real customer information or passwords. Email verification is not connected.'})
        set_cookie(response,anon_cookie,token,httponly=True,max_age=1800)
        return response

    @app.post('/api/auth/register')
    def register(data: RegisterIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        anon_guard(request,x_csrf_token)
        uid=checked(lambda:accounts.register_test_player(data.email,data.name,data.password))
        return {'ok':True,'test_account_id':uid,'message':'Fictional account created. No verification email was sent.'}

    @app.post('/api/auth/login')
    def login(data: LoginIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        anon_guard(request,x_csrf_token)
        peer=request.client.host if request.client else 'unknown'
        raw,csrf,uid=checked(lambda:accounts.login(data.email,data.password,peer))
        # Revoke any existing browser session when replacing its identity.
        previous=request.cookies.get(session_cookie)
        if previous:
            try:accounts.logout(accounts.authenticate(previous))
            except AuthenticationFailed:pass
        response=JSONResponse({'ok':True,'message':'Signed in to the fictional test account.','csrf':csrf})
        set_cookie(response,session_cookie,raw,httponly=True,max_age=SESSION_SECONDS)
        set_cookie(response,csrf_cookie,csrf,httponly=False,max_age=SESSION_SECONDS)
        response.delete_cookie(anon_cookie,path='/',secure=secure_cookies,httponly=True,samesite='strict')
        return response

    def session_profile(p: Principal,csrf: str):
        with db.read() as c:
            acknowledged=bool(c.execute('SELECT 1 FROM test_notices WHERE user_id=? AND version=?',(p.id,TEST_NOTICE_VERSION)).fetchone())
        return {'id':p.id,'email':p.email,'name':p.name,'role':p.role,'permissions':checked(lambda:accounts.permissions(p.id)),
                'csrf':csrf,'notice_acknowledged':acknowledged,'mode':'fictional-only'}

    @app.get('/api/auth/state')
    def auth_state(request: Request):
        try:p=accounts.authenticate(request.cookies.get(session_cookie))
        except AuthenticationFailed:return {'authenticated':False}
        csrf=request.cookies.get(csrf_cookie)
        if not accounts.csrf_ok(p,csrf,csrf):return {'authenticated':False}
        return {'authenticated':True,**session_profile(p,csrf)}

    @app.get('/api/auth/me')
    def me(request: Request):
        p=principal(request);csrf=request.cookies.get(csrf_cookie)
        if not accounts.csrf_ok(p,csrf,csrf):raise HTTPException(403,'Test CSRF cookie is missing; sign in again.')
        return session_profile(p,csrf)

    @app.post('/api/auth/logout')
    def logout(request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:accounts.logout(p))
        response=JSONResponse({'ok':True});clear_auth_cookies(response);return response

    @app.post('/api/auth/password')
    def password(data: PasswordIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:accounts.change_password(p,data.old_password,data.new_password))
        response=JSONResponse({'ok':True,'message':'Test password changed. All sessions were revoked.'});clear_auth_cookies(response);return response

    @app.get('/api/staff')
    def staff_list(request: Request):
        p=principal(request);superuser(p)
        return {'staff':checked(lambda:accounts.list_test_staff(p.id)),'grantable':sorted(accounts.grantable)}

    @app.post('/api/staff')
    def staff_create(data: StaffIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p)
        uid=checked(lambda:accounts.create_test_staff(p.id,data.email,data.name,data.password,data.role))
        return {'ok':True,'test_staff_id':uid,'message':'Fictional staff account created without permissions.'}

    @app.post('/api/staff/{staff_id}/permissions')
    def staff_permissions(staff_id: int,data: PermissionsIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p)
        checked(lambda:accounts.replace_permissions(p.id,staff_id,data.permissions))
        return {'ok':True,'message':'Fictional staff permissions updated.'}

    @app.post('/api/staff/{user_id}/disable')
    def staff_disable(user_id: int,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p)
        checked(lambda:accounts.disable_test_user(p.id,user_id));return {'ok':True}

    @app.get('/api/staff/audit')
    def audit(request: Request):
        p=principal(request);superuser(p)
        with db.read() as c:events=[dict(r) for r in c.execute('SELECT actor_id,target_id,event,created_at FROM auth_audit ORDER BY id DESC LIMIT 100')]
        return {'events':events}

    @app.get('/api/test/players')
    def players(request: Request):
        p=principal(request)
        if p.role in ('admin','manager'):checked(lambda:accounts.require_staff_permission(p.id,'bookings.view'))
        return {'players':accounts.test_players(),'mode':'fictional-only'}

    @app.get('/api/test/sessions')
    def sessions(request: Request):
        p=principal(request)
        if p.role in ('admin','manager'):checked(lambda:accounts.require_staff_permission(p.id,'session.view'))
        return {'sessions':accounts.test_sessions(),'mode':'fictional-only'}

    @app.post('/api/test/notice')
    def notice(request: Request,data: EmptyIn | None=None,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:accounts.accept_test_notice(p.id))
        return {'ok':True,'message':'Fictional notice acknowledged; this is not legal consent.'}

    @app.post('/api/test/checkout')
    def checkout(data: BookingIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token)
        result=checked(lambda:accounts.test_checkout(p.id,data.session_id,data.player_ids))
        return {'ok':True,**result,'message':'Simulated payment only; no real booking or charge.'}

    @app.get('/api/ops/test-bookings')
    def bookings(request: Request):
        p=principal(request)
        return {'bookings':checked(lambda:accounts.staff_bookings(p.id)),'mode':'fictional-only'}

    @app.post('/api/ops/test-rate')
    def rate(data: RateIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:accounts.set_test_open_rate(p.id,data.amount))
        return {'ok':True,'message':'Fictional rate updated.'}

    @app.post('/api/test/check-in')
    def checkin(data: CheckinIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token)
        cid=checked(lambda:engine.check_in(data.session_id,p.id,data.fictional_venue_token))
        return {'ok':True,'checkin_id':cid,'message':'Test token accepted; no real QR scan occurred.'}

    @app.get('/api/test/queue/{session_id}')
    def queue(session_id: int,request: Request):
        p=principal(request)
        if p.role in ('admin','manager'):checked(lambda:accounts.require_staff_permission(p.id,'queue.view'))
        if p.role=='player':
            with db.read() as c:
                if not c.execute('SELECT 1 FROM session_registrations WHERE session_id=? AND user_id=?',(session_id,p.id)).fetchone():
                    raise HTTPException(403,'A simulated registration is required.')
        return {'queue':engine.queue(session_id),'mode':'fictional-only'}

    @app.post('/api/test/queue/assign')
    def assign(data: AssignIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token)
        mid=checked(lambda:engine.assign_next(p.id,data.session_id,data.court))
        return {'ok':True,'match_id':mid,'message':'Provisional fictional queue assignment.'}

    @app.post('/api/test/matches/{match_id}/end')
    def end_match(match_id: int,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:engine.end_match(p.id,match_id))
        return {'ok':True}

    @app.post('/api/test/matches/{match_id}/score')
    def score(match_id: int,data: ScoreIn,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:engine.submit_score(p.id,match_id,data.score_a,data.score_b))
        return {'ok':True,'message':'Fictional score awaits both teams.'}

    @app.post('/api/test/matches/{match_id}/confirm')
    def confirm(match_id: int,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);confirmed=checked(lambda:engine.confirm_score(p.id,match_id))
        return {'ok':True,'confirmed':confirmed}

    @app.post('/api/test/matches/{match_id}/recording/start')
    def recording_start(match_id: int,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);result=checked(lambda:recorder.start(p.id,match_id))
        return {'ok':True,**result,'message':'Synthetic pattern only, not a camera.'}

    @app.post('/api/test/matches/{match_id}/recording/stop')
    def recording_stop(match_id: int,request: Request,x_csrf_token: str | None=Header(default=None)):
        p=action(request,x_csrf_token);result=checked(lambda:recorder.stop(p.id,match_id))
        return {'ok':True,**result}

    @app.get('/api/test/matches/{match_id}/replay')
    def replay(match_id: int,request: Request):
        p=principal(request);path=checked(lambda:engine.private_recording(p.id,match_id))
        if not path.is_file():raise HTTPException(404,'Synthetic test clip unavailable.')
        return FileResponse(path,media_type='video/mp4',headers={'Cache-Control':'private, no-store'})

    @app.get('/api/test/notices')
    def notices(request: Request):
        p=principal(request)
        with db.read() as c:
            rows=[dict(r) for r in c.execute('SELECT match_id,kind,subject,body FROM outbox WHERE user_id=? ORDER BY id DESC',(p.id,))]
        return {'notices':rows,'message':'Unsent test records; no email was sent.'}

    @app.get('/api/test/ranking')
    def ranking(request: Request):
        p=principal(request)
        if p.role in ('admin','manager'):checked(lambda:accounts.require_staff_permission(p.id,'rankings.view'))
        return {'ranking':engine.ranking(),'mode':'fictional-only'}

    @asynccontextmanager
    async def lifespan(application):
        try:yield
        finally:recorder.close()
    app.router.lifespan_context=lifespan
    return app
