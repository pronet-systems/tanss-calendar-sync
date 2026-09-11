"""Fehler der TANSS-Schnittstelle, die der Abgleich fachlich unterscheiden muss."""

from __future__ import annotations


class TanssError(RuntimeError):
    """Basis für alles, was die TANSS-API meldet."""

    def __init__(self, message: str, *, status: int | None = None,
                 detail: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class TanssAuthError(TanssError):
    """403 oder abgelaufenes Token.

    Häufigste Ursache im Betrieb: ein ``/api/v1``-Aufruf ohne ``loggedInUserId``.
    Ein ``TANSS_APP``-Token ist ohne diesen Parameter auf ``/api/tanss.x/v1`` beschränkt.
    """


class TanssNotFound(TanssError):
    """404 ``OBJECT_NOT_FOUND``.

    Für den Löschpfad die wichtigste Antwort überhaupt: Nur sie ist ein gültiger
    Löschnachweis (Invariante 2).
    """


class TanssDuplicateError(TanssError):
    """Der Server hat die Anlage wegen der Duplikatsprüfung abgelehnt.

    Das ist **erwünscht** — die zweite Verteidigungslinie gegen doppelte Termine.
    Der Aufrufer behandelt es als „existiert bereits" und verknüpft den vorhandenen
    Termin, statt es beim nächsten Lauf erneut zu versuchen.
    """


class ChangesDiscardedError(TanssError):
    """403 ``CHANGES_WERE_DISCARDED`` bei ``discardOnSupport=true``.

    Die Server-Auskunft „nicht mehr dein Objekt": Der Termin wurde inzwischen in eine
    getätigte Leistung gewandelt. Wird als *Kopplung beenden* verarbeitet, nie als Fehler
    und nie als Löschung (Invariante 5).
    """


class CompanyRequiredError(TanssError):
    """``COMPANY_MUST_BE_GIVEN`` — jeder Termin braucht eine Firma.

    Tritt nur auf, wenn weder eine Firma gesetzt noch der Eigene-Firma-Rückfall aktiv
    ist. Letzteres macht ausschließlich der ``/api/tanss.x/v1``-Endpunkt.
    """
