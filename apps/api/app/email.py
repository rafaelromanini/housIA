"""Envio de e-mail por SMTP configurável (mailpit no compose de dev). Sem SMTP_HOST só registra no log."""

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger("housia.email")

# Caixa de saída em memória: o que foi "enviado" neste processo (os testes leem o link daqui).
OUTBOX: list[dict] = []


def send_email(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    OUTBOX.append({"to": to, "subject": subject, "body": body})
    del OUTBOX[:-200]
    if not settings.smtp_host:
        logger.info("E-mail (sem SMTP configurado) para %s: %s", to, subject)
        return
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except OSError:  # SMTP fora do ar não derruba o cadastro: o usuário pede novo envio
        logger.exception("Falha ao enviar e-mail para %s", to)
