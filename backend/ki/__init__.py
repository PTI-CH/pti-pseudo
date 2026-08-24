"""KI-Schicht (Phase 3, Variante C — Infomaniak AI Tools).

Getrennt in zwei Bausteine: :mod:`backend.ki.infomaniak` redet HTTP,
:mod:`backend.ki.namen` redet Text.

Die Trennung ist kein Selbstzweck. Sie bestimmt, was ein Wechsel der
Ausbaustufe kostet — also der Entscheidung, **wieviel Text das Haus
verlaesst**. Heute gehen nur Fenster um Fundkandidaten hinaus; ganze Segmente
oder ganze Dokumente waeren die naechsten Stufen. Ein solcher Wechsel beruehrt
ausschliesslich ``namen``: Client, Schema und Fehlerbehandlung bleiben.

Die Abwaegung dazu steht in
``docs/superpowers/specs/2026-08-24-ki-schicht-namen-design.md``, Abschnitt 8.
"""
