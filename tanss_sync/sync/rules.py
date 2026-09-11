"""Die Ignorier- und Übernahmeregeln aus dem fachlichen Pflichtenheft.

Alle an einer Stelle — verteilt über Mapper und Reconciler wären sie nicht prüfbar.
Jede Regel gibt ihre Begründung zurück, denn die landet im Änderungsprotokoll und
beantwortet später die Frage „warum wurde der nicht übertragen?".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.models import SyncConfig
from ..domain.appointment import Appointment
from ..domain.identity import UserMapping
from ..state.records import LinkRecord

_DAY_MINUTES = 24 * 60


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


YES = Verdict(True, "")


class SyncRules:
    def __init__(self, config: SyncConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ Outlook → TANSS

    def should_sync_to_tanss(self, appointment: Appointment,
                             user: UserMapping) -> Verdict:
        if not user.direction.allows_to_tanss():
            return Verdict(False, "Richtung für diesen Benutzer nicht freigegeben")

        if not appointment.kind.syncs_to_tanss:
            return Verdict(False, f"{appointment.kind.value} geht nie zurück nach TANSS")

        if appointment.is_cancelled:
            return Verdict(False, "in Outlook abgesagt")

        if self.config.ignore_show_as_free and appointment.show_as == "free":
            # Absicht: So lassen sich Eintraege fuehren, die den Abgleich nichts angehen.
            return Verdict(False, "als Frei/Verfügbar markiert")

        if self.config.ignore_all_day and appointment.all_day:
            return Verdict(False, "als ganztägig markiert")

        # Fremdsysteme tragen Urlaub als 24-Stunden-Termin ein. Ein legitimer
        # Ganztagestermin ist laut Pflichtenheft 00:00-24:00 OHNE Ganztags-Haken und
        # damit ebenfalls 24 h lang - deshalb greift die Regel nur zusammen mit "Frei".
        if appointment.duration_minutes >= _DAY_MINUTES and appointment.show_as == "free":
            return Verdict(False, "24-Stunden-Eintrag mit Status Frei")

        cutoff = self._activation_verdict(appointment, user)
        if not cutoff:
            return cutoff

        return YES

    # ------------------------------------------------------------ TANSS → Outlook

    def should_sync_to_outlook(self, appointment: Appointment, user: UserMapping, *,
                               created_filtered: bool) -> Verdict:
        """``created_filtered`` sagt, ob die Abfrage bereits serverseitig gefiltert hat.

        Fehlt beides — kein serverseitiger Filter **und** kein bekanntes Anlagedatum —,
        wird **nicht** übertragen. Das ist bewusst streng: Der Anlagezeitpunkt steht
        nicht in der Listenantwort, und ohne ihn ließe sich Altbestand nicht von
        Neuem unterscheiden. Der erste Lauf würde dann alles übertragen, was im
        Zeitfenster liegt.
        """
        if not user.direction.allows_to_m365():
            return Verdict(False, "Richtung für diesen Benutzer nicht freigegeben")

        if not appointment.kind.syncs_to_outlook:
            return Verdict(False, f"{appointment.kind.value} geht nie nach Outlook")

        if appointment.kind.is_absence and not self.config.sync_absences:
            return Verdict(False, "Abwesenheiten sind abgeschaltet")

        if not appointment.start or not appointment.end:
            return Verdict(False, "ohne Start- oder Endzeit")

        if created_filtered:
            return YES

        if appointment.created is None:
            return Verdict(
                False,
                "Anlagezeitpunkt unbekannt und keine serverseitige Filterung — "
                "ohne beides ließe sich Altbestand nicht erkennen")

        return self._activation_verdict(appointment, user)

    # ------------------------------------------------------------ gemeinsam

    @staticmethod
    def _activation_verdict(appointment: Appointment, user: UserMapping) -> Verdict:
        if user.activated_at is None:
            return Verdict(False, "kein Aktivierungszeitpunkt hinterlegt")
        if appointment.created and appointment.created < user.activated_at:
            return Verdict(False, "vor der Aktivierung angelegt")
        return YES

    def newly_excluded(self, appointment: Appointment, link: LinkRecord) -> Verdict:
        """Ein **bereits gekoppelter** Termin wird unsynchronisierbar.

        Dann wird **entkoppelt, nicht gelöscht**. Ohne diese Regel wäre „Termin auf
        Frei setzen" ein stiller Löschbefehl für den TANSS-Datensatz.
        """
        if link.state != "linked":
            return Verdict(False, "")
        if appointment.is_cancelled:
            return Verdict(True, "in Outlook abgesagt — Kopplung beenden")
        if self.config.ignore_show_as_free and appointment.show_as == "free":
            return Verdict(True, "auf Frei gesetzt — Kopplung beenden")
        if self.config.ignore_all_day and appointment.all_day:
            return Verdict(True, "auf ganztägig gesetzt — Kopplung beenden")
        return Verdict(False, "")
