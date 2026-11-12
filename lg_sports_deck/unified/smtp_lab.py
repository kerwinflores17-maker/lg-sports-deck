"""Loopback-only STARTTLS/SMTP laboratory for fictional notification jobs.

This module can talk ONLY to an explicitly supplied 127.0.0.1 port using a
pinned *test* certificate, invented .invalid addresses and invented test-only
authentication. It cannot connect to Gmail or any external SMTP host. It does
not read credentials from the environment, send actual email, or run by itself.

Its purpose is to exercise the SMTP wire protocol and failure states against a
local fake sink before a future, separately authorized sender integration. A
fake sink's acceptance is NOT delivery to an inbox. A real SMTP implementation
will need real credential management, verified recipients, limits, monitoring,
legal/privacy review and further security testing; none is established here.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
import re
import smtplib
import ssl
from typing import Callable, Literal

from lg_sports_deck.accounts.service import normalize_test_email
from lg_sports_deck.core.domain import Database
from .notifications import SimulatedMailWorker, render_test_mime


OUTCOME=Literal['success','transient','permanent','unknown']
TEST_SECRET_RE=re.compile(r'^LG_FAKE_[A-Za-z0-9_-]{24,128}$',re.ASCII)


@dataclass(frozen=True)
class TestWireResult:
    outcome: OUTCOME
    stage: str  # fixed label only; no exception text or message data


def _test_mailbox(value: str) -> str:
    normalized=normalize_test_email(value)
    if normalized!=value:raise ValueError('Use a normalized fictional .invalid mailbox.')
    return normalized


def _only_mailbox(message: EmailMessage, name: str) -> str:
    values=message.get_all(name,[])
    if len(values)!=1:raise ValueError('A single fictional mailbox is required.')
    parsed=getaddresses([str(values[0])])
    if len(parsed)!=1 or parsed[0][0] or not parsed[0][1]:
        raise ValueError('Use a single plain fictional mailbox.')
    return _test_mailbox(parsed[0][1])


class LoopbackSMTP:
    """Manually send one fictional MIME message to a local fake SMTP sink.

    The target is hardcoded to the IPv4 loopback address: a caller cannot set a
    hostname, and this code rejects real email addresses and non-fixture secrets.
    A pinned CA is mandatory; STARTTLS and hostname verification are never
    bypassed. The synthetic sink must present a certificate for 127.0.0.1.
    """
    def __init__(self,port: int,test_ca_pem: str,username: str,test_password: str,*,timeout: float=4):
        if type(port) is not int or not 1<=port<=65535:
            raise ValueError('Choose a local fake SMTP port.')
        self.username=_test_mailbox(username)
        if not isinstance(test_password,str) or not TEST_SECRET_RE.fullmatch(test_password):
            raise ValueError('Only a generated LG_FAKE_ test secret is allowed.')
        if not isinstance(test_ca_pem,str) or not 1<=len(test_ca_pem)<=20000:
            raise ValueError('A pinned fake CA certificate is required.')
        if not isinstance(timeout,(float,int)) or isinstance(timeout,bool) or not 0.1<=timeout<=10:
            raise ValueError('Invalid test SMTP timeout.')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.check_hostname=True
        context.verify_mode=ssl.CERT_REQUIRED
        try:context.load_verify_locations(cadata=test_ca_pem)
        except ssl.SSLError as exc:raise ValueError('Invalid pinned fake CA certificate.') from exc
        self._context=context;self._port=port;self._password=test_password;self._timeout=float(timeout)

    @staticmethod
    def _response(code: int,stage: str) -> TestWireResult:
        return TestWireResult('transient' if 400<=code<500 else 'permanent',stage)

    @staticmethod
    def _message_bytes(message: EmailMessage,sender: str,recipient: str) -> bytes:
        if not isinstance(message,EmailMessage):raise ValueError('Use a fictional test MIME message.')
        sender=_test_mailbox(sender);recipient=_test_mailbox(recipient)
        if (_only_mailbox(message,'From')!=sender or _only_mailbox(message,'To')!=recipient
            or message.get_all('X-LG-Test-Only',[])!=['yes'] or message.get_all('Cc') or message.get_all('Bcc')
            or any(str(k).lower().startswith('resent-') for k in message.keys())):
            raise ValueError('The fictional MIME envelope does not match its headers.')
        raw=message.as_bytes(policy=SMTP)
        if len(raw)>262144:raise ValueError('The fictional message is too large.')
        return raw

    def submit(self,message: EmailMessage,*,sender: str,recipient: str) -> TestWireResult:
        """Submit to the fake sink and classify only conservative fixed outcomes.

        A connection loss during DATA may follow sink acceptance, so it is
        *unknown*, never automatically retried. 250 from the fake sink is merely
        a test acceptance. No raw SMTP error, credential or message is logged.
        """
        raw=self._message_bytes(message,sender,recipient)
        client=None
        try:
            try:
                client=smtplib.SMTP('127.0.0.1',self._port,timeout=self._timeout)
                code,_=client.ehlo()
                if code!=250 or not client.has_extn('starttls'):
                    return TestWireResult('permanent','test_tls_unavailable')
                client.starttls(context=self._context)
                code,_=client.ehlo()
                if code!=250 or not client.has_extn('auth'):
                    return TestWireResult('permanent','test_auth_unavailable')
                client.login(self.username,self._password)
            except smtplib.SMTPAuthenticationError as exc:
                return self._response(exc.smtp_code,'test_auth_rejected')
            except (ssl.SSLError,smtplib.SMTPNotSupportedError):
                return TestWireResult('permanent','test_tls_or_auth_failed')
            except smtplib.SMTPResponseException as exc:
                return self._response(exc.smtp_code,'test_setup_rejected')
            except (OSError,smtplib.SMTPException):
                return TestWireResult('transient','test_connection_failed')
            try:
                refused=client.sendmail(sender,[recipient],raw)
                if refused:return TestWireResult('permanent','test_recipient_rejected')
                return TestWireResult('success','fake_sink_accepted')
            except smtplib.SMTPRecipientsRefused as exc:
                replies=list(exc.recipients.values())
                code=replies[0][0] if len(replies)==1 and type(replies[0][0]) is int else 550
                return self._response(code,'test_recipient_rejected')
            except (smtplib.SMTPSenderRefused,smtplib.SMTPDataError) as exc:
                return self._response(exc.smtp_code,'test_smtp_rejected')
            except smtplib.SMTPResponseException as exc:
                return self._response(exc.smtp_code,'test_smtp_rejected')
            except (OSError,smtplib.SMTPException):
                # A lost connection/timeout while submitting cannot prove that
                # DATA was not accepted, even if a retry might appear helpful.
                return TestWireResult('unknown','test_submission_uncertain')
        finally:
            if client is not None:
                try:client.quit()
                except (OSError,smtplib.SMTPException):client.close()


class LoopbackSMTPWorker:
    """One manual job claim -> fictional MIME -> local fake TLS sink -> state.

    The simulated receipt marks fake acceptance only. The worker runs only if
    explicitly invoked by an internal test and has no external network route.
    It reuses the test outbox's lease/ambiguous-outcome controls; expired leases
    and uncertain submissions are not blindly retried. Not a live mail worker.
    """
    def __init__(self,db: Database,clock: Callable[[],float],transport: LoopbackSMTP,
                 *,sender: str,app_origin: str,lease_seconds: int=120,max_attempts: int=5,bucket: str='primary'):
        if type(transport) is not LoopbackSMTP:raise ValueError('Only the loopback fake SMTP transport is allowed.')
        self.transport=transport;self.sender=_test_mailbox(sender);self.app_origin=app_origin
        self.db=db;self.worker=SimulatedMailWorker(db,clock,lease_seconds=lease_seconds,max_attempts=max_attempts,bucket=bucket)

    def run_one(self) -> dict|None:
        claim=self.worker.claim()
        if not claim:return None
        try:
            message=render_test_mime(self.db,claim.job_id,self.app_origin,self.sender,bucket=claim.bucket)
            result=self.transport.submit(message,sender=self.sender,recipient=claim.recipient)
        except ValueError:
            result=TestWireResult('permanent','test_message_invalid')
        state=self.worker.finish(claim,result.outcome,fake_transport_attempted=True)
        return {'job_id':claim.job_id,'state':state,'test_outcome':result.outcome,
                'test_stage':result.stage,'mode':'fictional-loopback-only'}
