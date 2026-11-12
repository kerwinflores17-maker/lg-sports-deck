"""Explicitly gated Gmail SMTP *test-message adapter*, not a live mail worker.

This module is NOT mounted on any HTTP route, not started by the unified app,
and not connected to its fictional outbox. Importing or constructing it never
opens a socket. `send_one_test` can send ONE clearly labeled message to ONE
explicitly allowlisted address only when a caller passes explicit_send=True.
Never invoke it without the user's separate authorization to send real email.

Credentials must be provided privately in memory from a deployment secret store,
never in chat, committed files, test fixtures or logs. No credential or address
has been supplied to or used by this adapter. A sender was selected separately;
it is not embedded or configured here. Sender/recipient settings can be
replaced for a later deployment. Gmail App Password availability depends on the
Google account, 2-Step Verification and organization settings. SMTP acceptance
is NOT inbox delivery, and a disconnection during DATA is an unknown outcome.

This isolated adapter is not production certification: account verification,
templates, notification policy, durable real-mail outbox/worker, secret
rotation, abuse controls, delivery monitoring and security/privacy review are
still required. Do not attach it to the fictional prototype or customer data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
import re
import smtplib
import ssl
from typing import Literal


GMAIL_HOST='smtp.gmail.com'
GMAIL_STARTTLS_PORT=587
# Restrict the test adapter to simple ASCII dot-atom mailboxes. Quoted, Unicode,
# comments and display names are intentionally not accepted here.
LOCAL_RE=re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$",re.ASCII)
LABEL_RE=re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$',re.ASCII)


class TestSendingDisabled(Exception):
    """No explicit send authorization was provided; no connection was opened."""
    __test__=False


def _mailbox(value: str) -> str:
    if not isinstance(value,str) or not value.isascii() or len(value)>254 or value.count('@')!=1:
        raise ValueError('Provide one simple ASCII email address.')
    local,domain=value.split('@');labels=domain.split('.')
    if (not LOCAL_RE.fullmatch(local) or local.startswith('.') or local.endswith('.') or '..' in local
        or len(labels)<2 or any(not LABEL_RE.fullmatch(label) for label in labels)
        or not re.fullmatch(r'[A-Za-z]{2,63}',labels[-1])
        or labels[-1].lower() in {'invalid','test','example','localhost'}):
        raise ValueError('Provide one simple non-reserved email address.')
    return local+'@'+domain.lower()


@dataclass(frozen=True)
class GmailTestSettings:
    """Short-lived test settings, supplied privately; never persist/log them."""
    sender: str=field(repr=False)
    app_password: str=field(repr=False)
    approved_recipients: frozenset[str]=field(repr=False)

    def __post_init__(self):
        sender=_mailbox(self.sender)
        if (not isinstance(self.app_password,str) or not 8<=len(self.app_password)<=256
            or any(ord(ch)<32 or ord(ch)==127 for ch in self.app_password)):
            raise ValueError('A private application credential is required.')
        if not isinstance(self.approved_recipients,(set,frozenset)) or not 1<=len(self.approved_recipients)<=10:
            raise ValueError('Explicitly allowlist one to ten test recipients.')
        recipients=frozenset(_mailbox(r) for r in self.approved_recipients)
        object.__setattr__(self,'sender',sender)
        object.__setattr__(self,'approved_recipients',recipients)


@dataclass(frozen=True)
class GmailTestResult:
    outcome: Literal['accepted','transient','rejected','unknown']
    stage: str  # safe fixed code; no addresses, password, body or exception text


class GmailTestSender:
    """Manual, one-recipient Gmail test sender; disabled absent per-call consent."""
    def __init__(self,settings: GmailTestSettings,*,timeout: float=10):
        if type(settings) is not GmailTestSettings:raise ValueError('Explicit Gmail test settings are required.')
        if not isinstance(timeout,(float,int)) or isinstance(timeout,bool) or not 1<=timeout<=30:
            raise ValueError('Invalid test SMTP timeout.')
        self._settings=settings;self._timeout=float(timeout)

    @staticmethod
    def _response(code: int,stage: str) -> GmailTestResult:
        return GmailTestResult('transient' if 400<=code<500 else 'rejected',stage)

    def send_one_test(self,recipient: str,*,explicit_send: bool=False) -> GmailTestResult:
        """Send only the fixed, clearly labeled test message to an allowlisted inbox.

        `explicit_send=True` is a technical guard, not a substitute for user
        authorization. Never assume 250 means inbox delivery. An unknown result
        must not be automatically retried, as the message may have been accepted.
        This function is deliberately disconnected from all web routes and jobs.
        """
        if explicit_send is not True:
            raise TestSendingDisabled('A separate explicit test-send authorization is required.')
        recipient=_mailbox(recipient)
        if recipient not in self._settings.approved_recipients:
            raise ValueError('This test recipient is not explicitly allowlisted.')
        message=EmailMessage()
        message['From']=self._settings.sender;message['To']=recipient
        message['Subject']='[TEST ONLY] LG Sports Deck email connection'
        message['Date']=formatdate(localtime=False)
        message['Message-ID']=make_msgid(domain=self._settings.sender.split('@',1)[1])
        message['X-LG-Test-Only']='yes'
        message.set_content('This is a test notification from LG Sports Deck. '
                            'No real booking, payment, match or video is represented by this message.\n')
        raw=message.as_bytes(policy=SMTP)
        try:
            context=ssl.create_default_context()
            context.minimum_version=ssl.TLSVersion.TLSv1_2
        except (ssl.SSLError,OSError):
            return GmailTestResult('rejected','tls_setup_failed')
        # No caller-supplied host, TLS bypass, alternate recipient, or debug mode.
        client=None
        try:
            try:
                client=smtplib.SMTP(GMAIL_HOST,GMAIL_STARTTLS_PORT,timeout=self._timeout)
                code,_=client.ehlo()
                if code!=250 or not client.has_extn('starttls'):
                    return GmailTestResult('rejected','tls_unavailable')
                client.starttls(context=context)
                code,_=client.ehlo()
                if code!=250 or not client.has_extn('auth'):
                    return GmailTestResult('rejected','auth_unavailable')
                client.login(self._settings.sender,self._settings.app_password)
            except smtplib.SMTPAuthenticationError as exc:
                return self._response(exc.smtp_code,'authentication_rejected')
            except (ssl.SSLError,smtplib.SMTPNotSupportedError):
                return GmailTestResult('rejected','tls_or_auth_failed')
            except smtplib.SMTPResponseException as exc:
                return self._response(exc.smtp_code,'setup_rejected')
            except (OSError,smtplib.SMTPException):
                return GmailTestResult('transient','connection_failed_before_submission')
            try:
                refused=client.sendmail(self._settings.sender,[recipient],raw)
                if refused:return GmailTestResult('rejected','recipient_rejected')
                return GmailTestResult('accepted','smtp_accepted_not_inbox_delivery')
            except smtplib.SMTPRecipientsRefused as exc:
                values=list(exc.recipients.values())
                code=values[0][0] if len(values)==1 and type(values[0][0]) is int else 550
                return self._response(code,'recipient_rejected')
            except (smtplib.SMTPSenderRefused,smtplib.SMTPDataError) as exc:
                return self._response(exc.smtp_code,'smtp_rejected')
            except smtplib.SMTPResponseException as exc:
                return self._response(exc.smtp_code,'smtp_rejected')
            except (OSError,smtplib.SMTPException):
                return GmailTestResult('unknown','submission_uncertain')
        finally:
            if client is not None:
                try:client.quit()
                except (OSError,smtplib.SMTPException):client.close()
