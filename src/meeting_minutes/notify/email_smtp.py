from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..db import Person
from ..settings import Settings
from . import Message


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings):
        self.s = settings

    def can_reach(self, person: Person) -> bool:
        return bool(person.email)

    def build(self, person: Person, msg: Message) -> EmailMessage:
        em = EmailMessage()
        em["Subject"] = msg.subject
        em["From"] = self.s.smtp_from
        em["To"] = person.email
        text = msg.text + (f"\n\n{msg.link_label}: {msg.link}" if msg.link else "")
        em.set_content(text)
        if msg.html:
            html = msg.html + (f'<p><a href="{msg.link}">{msg.link_label}</a></p>' if msg.link else "")
            em.add_alternative(html, subtype="html")
        for a in msg.attachments:
            maintype, _, subtype = a.mime.partition("/")
            em.add_attachment(a.content, maintype=maintype, subtype=subtype or "octet-stream", filename=a.filename)
        return em

    def send(self, person: Person, msg: Message) -> None:
        em = self.build(person, msg)
        ctx = ssl.create_default_context()
        if self.s.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
        with server:
            if self.s.smtp_security == "starttls":
                server.starttls(context=ctx)
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password or "")
            server.send_message(em)
