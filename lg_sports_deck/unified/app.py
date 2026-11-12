"""Authenticated fictional booking/operations prototype. NOT for real users.

Reuses the account factory and the core DB/queue/recorder. All insecure legacy
Demo Superuser endpoints and older account-only test booking endpoints are
excluded: there is one identity source and one fictional booking ledger.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import time
from typing import Callable,Literal

from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, StrictBool, StrictInt

from lg_sports_deck.accounts.portal import Body, checked, create_app as create_account_app
from lg_sports_deck.core.domain import Conflict
from lg_sports_deck.core.recording import SyntheticFFmpegBackend
from .domain import EXTRA_GRANTABLE, UnifiedDemo
from .integration_settings_test import FictionalIntegrationSettings
from .manual_qr_lab import FictionalManualQR
from .player_alerts import FictionalPlayerAlerts
from .qr_test import FictionalQR
from .sample_retention_test import SampleAutoRetention
from .schedule_template import ScheduleTemplate

STATIC=Path(__file__).resolve().parent/'static'


class EmptyIn(Body):pass


class SessionIn(Body):
    day: date
    start_hour: StrictInt=Field(ge=6,le=23)
    duration: StrictInt=Field(ge=3,le=4)
    label: str=Field(min_length=1,max_length=80)
    fictional_venue_token: str=Field(min_length=6,max_length=256)


class OpenBookingIn(Body):
    session_id: StrictInt=Field(gt=0)
    player_ids: list[StrictInt]=Field(min_length=1,max_length=100)


class CourtBookingIn(Body):
    day: date
    start_hour: StrictInt=Field(ge=6,le=23)
    duration: StrictInt=Field(ge=1,le=18)
    court: StrictInt=Field(ge=1,le=2)
    player_ids: list[StrictInt]=Field(min_length=1,max_length=50)


class ManualOpenIn(OpenBookingIn):
    idempotency_key: str=Field(pattern=r'^[A-Za-z0-9_-]{16,100}$')


class ManualCourtIn(CourtBookingIn):
    idempotency_key: str=Field(pattern=r'^[A-Za-z0-9_-]{16,100}$')


class ManualClaimIn(Body):
    reference: str=Field(pattern=r'^FAKE-QR-[A-Za-z0-9_-]{8,80}$')


class ManualReviewIn(ManualClaimIn):
    observed_amount: StrictInt=Field(gt=0,le=100000000)
    account_checked: StrictBool


class SidIn(Body):session_id: StrictInt=Field(gt=0)
class NoShowIn(SidIn):player_id: StrictInt=Field(gt=0)
class CheckinIn(SidIn):fictional_venue_token: str=Field(min_length=1,max_length=256)
class TestQRCheckinIn(SidIn):challenge: str=Field(pattern=r'^[A-Za-z0-9_-]{32}$')
class AssignIn(SidIn):court: StrictInt=Field(ge=1,le=2)
class ReorderIn(SidIn):
    expected_ids: list[StrictInt]=Field(max_length=100)
    ordered_ids: list[StrictInt]=Field(max_length=100)
    expected_revision: str=Field(pattern=r'^[0-9a-f]{64}$')
class SelectedAssignIn(AssignIn):
    team_a: list[StrictInt]=Field(min_length=2,max_length=2)
    team_b: list[StrictInt]=Field(min_length=2,max_length=2)
class RatesIn(Body):
    open_rate: StrictInt=Field(ge=1,le=100000)
    court_rate: StrictInt=Field(ge=1,le=100000)
    paddle_rate: StrictInt=Field(ge=1,le=100000)
class LimitsIn(Body):
    open_limit: StrictInt=Field(ge=4,le=100)
    court_limit: StrictInt=Field(ge=1,le=50)
class RentalMatchIn(Body):
    booking_id: StrictInt=Field(gt=0)
    player_ids: list[StrictInt]=Field(min_length=4,max_length=4)
class ScoreIn(Body):
    score_a: StrictInt=Field(ge=0,le=99)
    score_b: StrictInt=Field(ge=0,le=99)
class TestSettingsRevisionIn(Body):
    expected_revision: StrictInt=Field(ge=0)
class TestSenderIn(TestSettingsRevisionIn):
    sender_email: str=Field(max_length=254)
class TestBookingRecipientIn(TestSettingsRevisionIn):
    recipient_email: str=Field(max_length=254)

class ScheduleSlotIn(Body):
    start_hour: StrictInt=Field(ge=6,le=23)
    duration: StrictInt=Field(ge=1,le=18)
    kind: str=Field(pattern=r'^(open_play|rental)$')
    label: str=Field(min_length=0,max_length=80)

class ScheduleTemplateIn(Body):
    slots: list[ScheduleSlotIn]

class ScheduleDayOverrideIn(ScheduleTemplateIn):
    day: date

class ScheduleDayClearIn(Body):
    day: date


def create_app(db_path: str|Path,media_path: str|Path,allowed_origins: set[str],
               *,secure_cookies: bool=True,clock: Callable[[],float]=time.time,
               ffmpeg_path: str|None=None,enable_sample_retention: bool=False,
               sample_retention_interval: float=60,
               sample_retention_now: Callable[[],datetime]=lambda:datetime.now(timezone.utc),
               enable_fictional_manual_qr: bool=False):
    if type(enable_fictional_manual_qr) is not bool:raise ValueError('Explicit fictional manual-QR flag must be boolean.')
    app=create_account_app(db_path,media_path,allowed_origins,secure_cookies=secure_cookies,
                           clock=clock,ffmpeg_path=ffmpeg_path,extra_grantable=EXTRA_GRANTABLE)
    # The previous account prototype contains standalone test checkout/ops
    # routes. Remove them here so they cannot bypass unified schedule/ledger
    # rules. The old profile-switch demo is never mounted at all.
    app.router.routes=[r for r in app.router.routes if getattr(r,'path',None)!='/' and
                       not str(getattr(r,'path','')).startswith(('/api/test/','/api/ops/'))]
    app.mount('/unified-static',StaticFiles(directory=STATIC),name='unified-static')
    accounts=app.state.accounts;engine=app.state.engine;recorder=app.state.recorder
    # For this unified *fictional* portal only, loop labelled stock footage in
    # place of the generic core test bars. No camera, RTSP or user video is used.
    if ffmpeg_path:
        recorder.backend=SyntheticFFmpegBackend(ffmpeg_path,sample_video=Path(__file__).resolve().parent/'assets'/'sample-stock-match.mp4')
    service=UnifiedDemo(engine.db,accounts,clock);app.state.unified=service
    schedule_template=ScheduleTemplate(engine.db,accounts);app.state.schedule_template=schedule_template
    # OFF by default. This mode requires the player's fictional claim and never
    # connects a real QR, account, transaction, email address or payment system.
    manual_test=FictionalManualQR(service,require_player_claim=True) if enable_fictional_manual_qr else None
    if manual_test is None:
        # Fail closed if a disposable test DB contains outstanding test claims;
        # silently re-enabling instant checkout would hide those requests.
        with engine.db.read() as c:
            present=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_test_manual_qr_intents'").fetchone()
            if present and c.execute("SELECT 1 FROM unified_test_manual_qr_intents WHERE state IN ('pending','manual_review') LIMIT 1").fetchone():
                raise RuntimeError('Outstanding fictional manual-QR requests require the explicitly enabled fictional test mode.')
    app.state.test_manual_qr=manual_test
    # Completely opt-in, sample-only retention experiment. It requires an
    # initially empty dedicated media folder and never backfills old clips.
    if type(enable_sample_retention) is not bool:
        raise ValueError('Fictional sample retention must be explicitly enabled or disabled.')
    app.state.test_sample_retention=None
    if enable_sample_retention:
        if (not ffmpeg_path or not isinstance(sample_retention_interval,(int,float)) or isinstance(sample_retention_interval,bool)
            or not 0.05<=sample_retention_interval<=3600 or not callable(sample_retention_now)):
            raise ValueError('Explicit fictional recorder and safe test worker settings are required.')
        retention=SampleAutoRetention(engine,media_path)
        recorder.test_ready_hook=retention.schedule_in_ready_transaction
        app.state.test_sample_retention=retention
        previous_lifespan=app.router.lifespan_context
        @asynccontextmanager
        async def fictional_sample_lifespan(application):
            async with previous_lifespan(application):
                stop=asyncio.Event()
                task=asyncio.create_task(retention.run_periodically(stop,interval=sample_retention_interval,now=sample_retention_now))
                try:yield
                finally:
                    stop.set()
                    try:await asyncio.wait_for(task,timeout=10)
                    except asyncio.TimeoutError:task.cancel()
        app.router.lifespan_context=fictional_sample_lifespan
    player_alerts=FictionalPlayerAlerts(service);app.state.test_player_alerts=player_alerts
    test_qr=FictionalQR(service);app.state.test_qr=test_qr
    test_integration_settings=FictionalIntegrationSettings(service);app.state.test_integration_settings=test_integration_settings
    principal=app.state.require_principal;action=app.state.require_action;superuser=app.state.require_superuser

    def require_unified_session(session_id: int):
        with engine.db.read() as c:
            if not c.execute('SELECT 1 FROM unified_sessions WHERE session_id=?',(session_id,)).fetchone():
                raise HTTPException(404,'Fictional unified session not found.')

    @app.get('/')
    def index():return FileResponse(STATIC/'index.html',media_type='text/html')

    @app.get('/api/u/catalog')
    def catalog(request: Request):
        p=principal(request);data=checked(lambda:service.catalog(p.id))
        retention=app.state.test_sample_retention
        data['sample_auto_retention_test_enabled']=retention is not None
        data['sample_auto_retention_test_status']=retention.last_sweep_status if retention else 'disabled'
        data['fictional_manual_qr_enabled']=manual_test is not None
        return data

    @app.post('/api/u/notice/accept')
    def accept_notice(request: Request,data: EmptyIn|None=None,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:accounts.accept_test_notice(p.id))
        return {'ok':True,'message':'Fictional recording notice acknowledged. This is not legal consent.'}

    @app.post('/api/u/sessions')
    def create_session(data: SessionIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        sid=checked(lambda:service.create_session(p.id,data.day,data.start_hour,data.duration,data.label,data.fictional_venue_token))
        return {'ok':True,'session_id':sid,'message':'Fictional Open Play session created; both test courts are blocked.'}

    @app.post('/api/u/settings/rates')
    def set_rates(data: RatesIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:service.set_rates(p.id,data.open_rate,data.court_rate,data.paddle_rate))
        return {'ok':True,'message':'Fictional rates updated.'}

    @app.post('/api/u/settings/limits')
    def set_limits(data: LimitsIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:service.set_limits(p.id,data.open_limit,data.court_limit))
        return {'ok':True,'message':'Fictional limits updated. New Open Play sessions use the new default.'}

    @app.get('/api/u/test-integration-settings')
    def view_test_integration_settings(request: Request,response: Response):
        p=principal(request);superuser(p);response.headers['Cache-Control']='no-store'
        return checked(lambda:test_integration_settings.view(p.id))

    @app.post('/api/u/test-integration-settings/sender')
    def replace_test_sender(data: TestSenderIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p);response.headers['Cache-Control']='no-store'
        return checked(lambda:test_integration_settings.replace_sender(p.id,data.expected_revision,data.sender_email))

    @app.post('/api/u/test-integration-settings/booking-recipient')
    def replace_test_booking_recipient(data: TestBookingRecipientIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p);response.headers['Cache-Control']='no-store'
        return checked(lambda:test_integration_settings.replace_booking_recipient(p.id,data.expected_revision,data.recipient_email))

    @app.get('/api/u/test-venue-booking-notices')
    def view_test_venue_notices(request: Request,response: Response):
        p=principal(request);superuser(p);response.headers['Cache-Control']='no-store'
        return {'notices':checked(lambda:test_integration_settings.venue_notices(p.id)),'mode':'fictional-only'}

    @app.post('/api/u/test-integration-settings/payment-qr')
    def replace_test_payment_qr(data: TestSettingsRevisionIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);superuser(p);response.headers['Cache-Control']='no-store'
        return checked(lambda:test_integration_settings.replace_payment_qr(p.id,data.expected_revision))

    @app.post('/api/u/book/openplay')
    def book_open(data: OpenBookingIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        if manual_test is not None:raise HTTPException(409,'Instant test checkout is disabled in fictional manual-review mode.')
        result=checked(lambda:service.book_openplay(p.id,data.session_id,data.player_ids))
        return {'ok':True,**result,'message':'Simulated full payment only; no real booking, GCash charge or email.'}

    @app.post('/api/u/book/court')
    def book_court(data: CourtBookingIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        if manual_test is not None:raise HTTPException(409,'Instant test checkout is disabled in fictional manual-review mode.')
        result=checked(lambda:service.book_court(p.id,data.day,data.start_hour,data.duration,data.court,data.player_ids))
        return {'ok':True,**result,'message':'Simulated full payment only; no real booking, GCash charge or email.'}

    if manual_test is not None:
        @app.post('/api/u/test-manual-qr/openplay')
        def test_manual_open(data: ManualOpenIn,request: Request,x_csrf_token: str|None=Header(default=None)):
            p=action(request,x_csrf_token)
            result=checked(lambda:manual_test.initiate(p.id,data.idempotency_key,'openplay',
                {'session_id':data.session_id,'player_ids':data.player_ids}))
            return {'ok':True,**result,'message':'Fictional request pending; no real payment or slot hold.'}

        @app.post('/api/u/test-manual-qr/court')
        def test_manual_court(data: ManualCourtIn,request: Request,x_csrf_token: str|None=Header(default=None)):
            p=action(request,x_csrf_token)
            result=checked(lambda:manual_test.initiate(p.id,data.idempotency_key,'court',
                {'day':data.day.isoformat(),'start_hour':data.start_hour,'duration':data.duration,
                 'court':data.court,'player_ids':data.player_ids}))
            return {'ok':True,**result,'message':'Fictional request pending; no real payment or slot hold.'}

        @app.get('/api/u/test-manual-qr/mine')
        def test_manual_mine(request: Request):
            p=principal(request);return checked(lambda:manual_test.list_mine(p.id))

        @app.get('/api/u/test-manual-qr/review')
        def test_manual_review_queue(request: Request):
            p=principal(request);return checked(lambda:manual_test.list_for_review(p.id))

        @app.post('/api/u/test-manual-qr/{intent_id}/claim')
        def test_manual_claim(intent_id: int,data: ManualClaimIn,request: Request,x_csrf_token: str|None=Header(default=None)):
            p=action(request,x_csrf_token)
            result=checked(lambda:manual_test.submit_claim(p.id,intent_id,data.reference))
            return {'ok':True,**result,'message':'Fictional claim saved. This is not proof of payment.'}

        @app.post('/api/u/test-manual-qr/{intent_id}/review')
        def test_manual_review(intent_id: int,data: ManualReviewIn,request: Request,x_csrf_token: str|None=Header(default=None)):
            p=action(request,x_csrf_token)
            result=checked(lambda:manual_test.confirm_account_check(p.id,intent_id,data.reference,
                data.observed_amount,account_checked=data.account_checked))
            return {'ok':True,**result,'message':('Fictional booking confirmed; test email jobs remain unsent.'
                if result['state']=='confirmed' else 'Fictional payment requires exception review; no booking was created.')}

    @app.get('/api/u/my-bookings')
    def my_bookings(request: Request):
        p=principal(request);return {'bookings':checked(lambda:service.my_bookings(p.id)),'mode':'fictional-only'}

    @app.get('/api/u/ops/bookings')
    def all_bookings(request: Request):
        p=principal(request);return {'bookings':checked(lambda:service.staff_bookings(p.id)),'mode':'fictional-only'}

    @app.get('/api/u/ops/roster/{session_id}')
    def roster(session_id: int,request: Request):
        p=principal(request);require_unified_session(session_id)
        return {'players':checked(lambda:service.roster(p.id,session_id)),'mode':'fictional-only'}

    @app.post('/api/u/waitlist')
    def waitlist(data: SidIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:service.join_waitlist(p.id,data.session_id))
        return {'ok':True,'message':'Added to the free fictional waiting list. No email was sent.'}

    @app.post('/api/u/ops/no-show')
    def no_show(data: NoShowIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);count=checked(lambda:service.mark_no_show(p.id,data.session_id,data.player_id))
        return {'ok':True,'notices':count,'message':f'Fictional no-show recorded; {count} unsent test notices created. No refund or email.'}

    @app.get('/api/u/ops/sales')
    def sales(request: Request,period: Literal['daily','monthly']='daily'):
        p=principal(request);return checked(lambda:service.sales(p.id,period))

    @app.get('/api/u/notices')
    def notices(request: Request):
        p=principal(request);return checked(lambda:service.notices(p.id))

    @app.get('/api/u/player-alerts')
    def get_player_alerts(request: Request,response: Response):
        p=principal(request);response.headers['Cache-Control']='no-store'
        return checked(lambda:player_alerts.list(p.id))

    @app.post('/api/u/player-alerts/{alert_id}/read')
    def read_player_alert(alert_id: int,data: EmptyIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:player_alerts.mark_read(p.id,alert_id))
        response.headers['Cache-Control']='no-store'
        return {'ok':True,'mode':'fictional-only'}

    @app.post('/api/u/check-in')
    def checkin(data: CheckinIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);require_unified_session(data.session_id)
        cid=checked(lambda:engine.check_in(data.session_id,p.id,data.fictional_venue_token))
        return {'ok':True,'checkin_id':cid,'message':'Fictional check-in complete; no QR was scanned.'}

    @app.post('/api/u/test-qr/issue')
    def issue_test_qr(data: SidIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        result=checked(lambda:test_qr.issue(p.id,data.session_id))
        response.headers['Cache-Control']='no-store'
        return result

    @app.post('/api/u/test-qr/check-in')
    def scan_test_qr(data: TestQRCheckinIn,request: Request,response: Response,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        cid,created=checked(lambda:test_qr.redeem(p.id,data.session_id,data.challenge))
        response.headers['Cache-Control']='no-store'
        return {'ok':True,'checkin_id':cid,'created':created,
                'message':'Fictional QR check-in complete.' if created else 'This fictional account is already checked in.'}

    @app.get('/api/u/queue/{session_id}')
    def queue(session_id: int,request: Request):
        p=principal(request);rows=checked(lambda:service.queue(p.id,session_id))
        return {'queue':rows,'revision':service.queue_revision(session_id,rows)}

    @app.post('/api/u/queue/reorder')
    def reorder(data: ReorderIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token)
        checked(lambda:service.reorder_queue(p.id,data.session_id,data.expected_ids,data.ordered_ids,data.expected_revision))
        return {'ok':True,'message':'Fictional waiting order updated. Provisional grouping still applies.'}

    @app.post('/api/u/queue/assign')
    def assign(data: AssignIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);require_unified_session(data.session_id)
        mid=checked(lambda:engine.assign_next(p.id,data.session_id,data.court))
        return {'ok':True,'match_id':mid,'message':'Provisional fictional match assigned.'}

    @app.post('/api/u/queue/assign-selected')
    def assign_selected(data: SelectedAssignIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);require_unified_session(data.session_id)
        mid=checked(lambda:engine.assign_selected(p.id,data.session_id,data.court,data.team_a,data.team_b))
        return {'ok':True,'match_id':mid,'message':'Fictional players and partners assigned.'}

    @app.get('/api/u/matches')
    def matches(request: Request):
        p=principal(request);return {'matches':checked(lambda:service.matches(p.id)),'mode':'fictional-only'}

    @app.post('/api/u/rental-match')
    def rental_match(data: RentalMatchIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);mid=checked(lambda:service.create_rental_match(p.id,data.booking_id,data.player_ids))
        return {'ok':True,'match_id':mid,'message':'Fictional rental match created from booked accounts.'}

    @app.post('/api/u/matches/{match_id}/end')
    def end_match(match_id: int,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:engine.end_match(p.id,match_id))
        return {'ok':True,'message':'Fictional match ended. Open Play players return to queue even if scores are pending.'}

    @app.post('/api/u/matches/{match_id}/score')
    def score(match_id: int,data: ScoreIn,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);checked(lambda:engine.submit_score(p.id,match_id,data.score_a,data.score_b))
        return {'ok':True,'message':'Fictional score awaits both teams.'}

    @app.post('/api/u/matches/{match_id}/confirm')
    def confirm(match_id: int,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);confirmed=checked(lambda:engine.confirm_score(p.id,match_id))
        return {'ok':True,'confirmed':confirmed,'message':'Both test teams confirmed.' if confirmed else 'One test team confirmed; the other is pending.'}

    @app.post('/api/u/matches/{match_id}/recording/start')
    def start(match_id: int,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);result=checked(lambda:recorder.start(p.id,match_id))
        return {'ok':True,**result,'message':'Playing a labelled stock sample only; no camera or actual match is being recorded.'}

    @app.post('/api/u/matches/{match_id}/recording/stop')
    def stop(match_id: int,request: Request,x_csrf_token: str|None=Header(default=None)):
        p=action(request,x_csrf_token);result=checked(lambda:recorder.stop(p.id,match_id))
        return {'ok':True,**result,'message':'Sample stock replay finalized; not your match footage.' if result['state']=='ready' else 'Sample recorder failed; no replay is ready.'}

    @app.get('/api/u/matches/{match_id}/replay')
    def replay(match_id: int,request: Request):
        p=principal(request);path=checked(lambda:engine.private_recording(p.id,match_id))
        if not path.is_file():raise HTTPException(404,'Synthetic clip unavailable.')
        return FileResponse(path,media_type='video/mp4',headers={'Cache-Control':'private, no-store'})

    @app.get('/api/u/ranking')
    def ranking(request: Request):
        p=principal(request)
        if p.role in ('admin','manager'):checked(lambda:accounts.require_staff_permission(p.id,'rankings.view'))
        return {'ranking':engine.ranking(),'mode':'fictional-only'}

    @app.get('/api/u/schedule/template')
    def get_schedule_template(request: Request):
        p=principal(request)
        return {'slots': checked(lambda: schedule_template.get_template()), 'mode': 'fictional-only'}

    @app.post('/api/u/schedule/template')
    def set_schedule_template(data: ScheduleTemplateIn, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.set_template(p.id, [s.model_dump() for s in data.slots]))
        return {'ok': True, 'message': 'Fictional daily schedule updated. Applies to all future days until overridden.'}

    @app.put('/api/u/schedule/template')
    def set_schedule_template_put(data: ScheduleTemplateIn, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.set_template(p.id, [s.model_dump() for s in data.slots]))
        return {'ok': True, 'message': 'Fictional daily schedule updated. Applies to all future days until overridden.'}

    @app.get('/api/u/schedule/day')
    def get_day_schedule(request: Request, day: date):
        p=principal(request)
        return {'day': day.isoformat(), 'slots': checked(lambda: schedule_template.get_day_schedule(day)), 'mode': 'fictional-only'}

    @app.post('/api/u/schedule/day')
    def set_day_override(data: ScheduleDayOverrideIn, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.set_day_override(p.id, data.day, [s.model_dump() for s in data.slots]))
        return {'ok': True, 'message': f'Fictional override saved for {data.day.isoformat()}.'}

    @app.put('/api/u/schedule/day')
    def set_day_override_put(data: ScheduleDayOverrideIn, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.set_day_override(p.id, data.day, [s.model_dump() for s in data.slots]))
        return {'ok': True, 'message': f'Fictional override saved for {data.day.isoformat()}.'}

    @app.post('/api/u/schedule/day/clear')
    def clear_day_override_post(data: ScheduleDayClearIn, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.clear_day_override(p.id, data.day))
        return {'ok': True, 'message': f'Fictional override cleared for {data.day.isoformat()}.'}

    @app.delete('/api/u/schedule/day')
    def clear_day_override(day: date, request: Request, x_csrf_token: str|None=Header(default=None)):
        p=action(request, x_csrf_token)
        checked(lambda: schedule_template.clear_day_override(p.id, day))
        return {'ok': True, 'message': f'Fictional override cleared for {day.isoformat()}.'}

    @app.get('/api/u/ops/sales/by-schedule')
    def sales_by_schedule(request: Request, period: Literal['daily','monthly']='daily', day: date | None=None):
        p=principal(request)
        target = day or service.now().date()
        return {'period': period, 'day': target.isoformat() if isinstance(target, date) else str(target),
                'by_schedule': checked(lambda: schedule_template.sales_by_schedule(p.id, period, target)),
                'mode': 'fictional-only'}

    return app
