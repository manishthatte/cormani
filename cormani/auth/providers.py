# SPDX-License-Identifier: GPL-3.0-or-later
#
# What each provider's endpoints and hostnames are.
#
# A registry, because CONVENTIONS.txt §4 says adding a provider should be one
# file plus one line rather than a branch inside something. Everything a
# provider needs is data; the code that uses it does not know which one it has.
#
# NO CLIENT ID OR SECRET APPEARS HERE. They are per-installation — a Google
# Cloud project and an Azure app registration that the person running corMani
# creates — and a credential in a source file is a credential in the
# repository. They go to the keyring under the provider's name, which is what
# `credentials.py` reads. That also means this file is publishable and the
# registration is not, which is the correct split for a GPL application whose
# users each register their own.
#
# The hostnames are recorded rather than inferred for the reason the schema
# gives: a provider's hostname is a fact about today, and an account whose host
# moves must be fixable without a new release. These are the DEFAULTS offered
# when an account is created; `account.imap_host` is what is actually used.
#
# © Manish Jagdish Thatte
from __future__ import annotations

from dataclasses import dataclass

METHOD_OAUTH2 = "oauth2"
METHOD_PASSWORD = "password"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    # Whether the provider files a copy in Sent BY ITSELF when a message is
    # submitted over SMTP. Google and Microsoft both do; an ordinary IMAP server
    # does not, and the client must APPEND one. Getting this backwards gives
    # either two copies of every sent message or none, and both are the kind of
    # thing a person only notices a week later.
    files_sent: bool = False
    authorize_url: str = ""
    token_url: str = ""
    # Mail scopes only. The calendar ones arrive with stage 5 and are listed
    # separately, because asking for calendar access while setting up mail is
    # how a consent screen gets refused.
    mail_scopes: tuple = ()
    calendar_scopes: tuple = ()
    # Whether an app password over LOGIN is an option at all.
    allows_password: bool = True

    @property
    def supports_oauth(self) -> bool:
        return bool(self.authorize_url and self.token_url)

    def scopes(self, *, calendar: bool = False) -> tuple:
        return self.mail_scopes + (self.calendar_scopes if calendar else ())


GOOGLE = Provider(
    name="google",
    label="Google",
    imap_host="imap.gmail.com",
    smtp_host="smtp.gmail.com",
    files_sent=True,
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    # The full-mailbox scope. Google offers narrower gmail.* scopes through the
    # Gmail API, and none of them work over IMAP — a client speaking IMAP needs
    # this one or nothing.
    mail_scopes=("https://mail.google.com/",),
    calendar_scopes=("https://www.googleapis.com/auth/calendar",),
    allows_password=True,
)

MICROSOFT = Provider(
    name="microsoft",
    label="Microsoft",
    imap_host="outlook.office365.com",
    smtp_host="smtp.office365.com",
    files_sent=True,
    authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    # offline_access is what makes a refresh token appear at all; without it
    # the sign-in has to be repeated every hour.
    mail_scopes=("https://outlook.office.com/IMAP.AccessAsUser.All",
                 "https://outlook.office.com/SMTP.Send",
                 "offline_access"),
    calendar_scopes=("https://graph.microsoft.com/Calendars.ReadWrite",),
    # Microsoft withdrew basic authentication for personal and work accounts.
    # Offering it would be offering something that cannot work.
    allows_password=False,
)

IMAP = Provider(
    name="imap",
    label="Other (IMAP)",
    # No endpoints and no hosts: an ordinary IMAP account is whatever the user
    # types, and a password is the only mechanism.
    allows_password=True,
)

FASTMAIL = Provider(
    name="fastmail",
    label="Fastmail",
    imap_host="imap.fastmail.com",
    smtp_host="smtp.fastmail.com",
    allows_password=True,
)

YAHOO = Provider(
    name="yahoo",
    label="Yahoo Mail",
    imap_host="imap.mail.yahoo.com",
    smtp_host="smtp.mail.yahoo.com",
    allows_password=True,
)

ICLOUD = Provider(
    name="icloud",
    label="iCloud Mail",
    imap_host="imap.mail.me.com",
    smtp_host="smtp.mail.me.com",
    allows_password=True,
)

PROVIDERS: dict = {p.name: p for p in (
    GOOGLE, MICROSOFT, FASTMAIL, YAHOO, ICLOUD, IMAP)}


def get(name: str) -> Provider:
    """The provider by name, falling back to plain IMAP.

    A fallback rather than an error: an account row naming a provider this
    build does not know is a configuration corMani should still be able to open
    and let the user fix, not one that stops the application starting.
    """
    return PROVIDERS.get((name or "").strip().lower(), IMAP)


def default_method(name: str) -> str:
    provider = get(name)
    return METHOD_OAUTH2 if provider.supports_oauth else METHOD_PASSWORD
