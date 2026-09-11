"""HTML ↔ Klartext — und zwei Blöcke, die dabei überleben müssen.

TANSS speichert ``text`` als Klartext, Graph liefert ``body.content`` in aller Regel als
HTML. Ohne Umwandlung landet HTML-Quelltext im TANSS-Termin, und der Inhalts-Hash
wechselt bei jedem Lauf, weil beide Seiten Verschiedenes halten — die Änderungserkennung
wäre dauerhaft instabil.

Zwei Dinge im Text sind **funktional** und dürfen nie verloren gehen:

* Der **Teams-Meeting-Block**. Wer ihn entfernt, macht das Online-Meeting unbrauchbar.
* Ein **TANSS-Hash** aus dem Outlook-Add-in. Wir bauen das Add-in nicht nach, aber wer es
  parallel nutzt, verliert sonst die Zuordnung seiner Termin-Parameter.
"""

from __future__ import annotations

import re

# Der Add-in-Marker im Termintext, z.B. "TANSS:743bd8b6-0eb7-4a58-955a-b0bf1ac4f439"
TANSS_HASH_RE = re.compile(r"TANSS:[0-9a-fA-F-]{16,}")

# Beginn des Teams-Blocks in beiden Sprachen; danach folgt bis zum Ende der Block.
_TEAMS_MARKERS = (
    "________________________________________________________________________________",
    "Microsoft Teams-Besprechung",
    "Microsoft Teams meeting",
    "Microsoft Teams Need help?",
)
_TEAMS_URL_RE = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/\S+")


class HtmlText:
    """Wandelt zwischen den beiden Darstellungen und erhält die funktionalen Blöcke."""

    def __init__(self) -> None:
        self._converter = None

    # ---------------------------------------------------------------- HTML -> Text

    def to_text(self, html: str | None) -> str:
        if not html:
            return ""
        if "<" not in html:  # schon Klartext
            return html.strip()
        return self._convert(html).strip()

    def _convert(self, html: str) -> str:
        if self._converter is None:
            try:
                import html2text

                conv = html2text.HTML2Text()
                conv.body_width = 0  # keine harten Umbrueche - sonst wandert der Hash
                conv.ignore_images = True
                conv.ignore_emphasis = True
                conv.protect_links = True  # URLs nicht in Markdown-Klammern zerlegen
                self._converter = conv
            except ImportError:  # pragma: no cover - Notbehelf ohne html2text
                self._converter = False
        if self._converter is False:
            return re.sub(r"<[^>]+>", "", html)
        return self._converter.handle(html)

    # ---------------------------------------------------------------- Text -> HTML

    @staticmethod
    def to_html(text: str | None) -> str:
        if not text:
            return ""
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        return "<html><body>" + escaped.replace("\n", "<br>") + "</body></html>"

    # ---------------------------------------------------------------- Blöcke

    @staticmethod
    def extract_tanss_hash(text: str | None) -> str | None:
        """Findet den Add-in-Marker, falls vorhanden."""
        if not text:
            return None
        match = TANSS_HASH_RE.search(text)
        return match.group(0) if match else None

    @staticmethod
    def extract_teams_url(text: str | None) -> str | None:
        if not text:
            return None
        match = _TEAMS_URL_RE.search(text)
        return match.group(0) if match else None

    @classmethod
    def split_meeting_block(cls, text: str | None) -> tuple[str, str]:
        """Trennt den eigentlichen Text vom Teams-Block.

        Damit lässt sich der Text ändern, ohne den Block anzufassen.
        """
        if not text:
            return "", ""
        for marker in _TEAMS_MARKERS:
            index = text.find(marker)
            if index > 0:
                return text[:index].rstrip(), text[index:]
        return text, ""

    @classmethod
    def preserve_blocks(cls, old_text: str | None, new_text: str) -> str:
        """Setzt den neuen Text zusammen und rettet Teams-Block und TANSS-Hash.

        Beides wird nur angehängt, wenn es im neuen Text fehlt — sonst entstünden
        Dubletten, die bei jedem Lauf weiterwachsen.
        """
        result = new_text.rstrip()

        _, teams_block = cls.split_meeting_block(old_text)
        if teams_block and teams_block not in result:
            result = f"{result}\n\n{teams_block.strip()}"

        old_hash = cls.extract_tanss_hash(old_text)
        if old_hash and old_hash not in result:
            result = f"{result}\n\n{old_hash}"

        return result.strip()

    @staticmethod
    def first_line(text: str | None) -> str:
        """Erste nichtleere Zeile — der Betreff-Rückfall für TANSS-Termine.

        ``outlookTitle`` ist bei allem leer, was in TANSS entsteht. Ohne diesen
        Rückfall bekäme jeder daraus erzeugte Outlook-Termin einen leeren Betreff.
        """
        if not text:
            return ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""
